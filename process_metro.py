import io
import os
import json
import shutil
import zipfile
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests

GTFS_URL = "https://api.transitous.org/gtfs/es_Metro-de-Valencia.gtfs.zip"
OUTPUT_DIR = "data"
OUTPUT_FILE = "metro_schedule.json"

def time_to_minutes(time_str: str) -> int:
    """Convierte 'HH:MM:SS' a minutos acumulados (24:01:00 -> 1441)."""
    if pd.isna(time_str):
        return -1
    parts = str(time_str).strip().split(":")
    if len(parts) >= 2:
        return int(parts[0]) * 60 + int(parts[1])
    return -1

def clean_text(val: str) -> str:
    """Normaliza texto Unicode y elimina espacios sobrantes."""
    if pd.isna(val):
        return ""
    normalized = unicodedata.normalize("NFKC", str(val))
    return " ".join(normalized.split())

def load_csv(zf: zipfile.ZipFile, filename: str) -> pd.DataFrame:
    """Carga CSV limpiando posibles espacios o caracteres BOM en las cabeceras."""
    df = pd.read_csv(zf.open(filename), dtype=str)
    df.columns = df.columns.str.strip().str.replace('\ufeff', '')
    return df

def main():
    tz = ZoneInfo("Europe/Madrid")
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(2)]
    print(f"Generando previsión para los días: {target_dates}")

    resp = requests.get(GTFS_URL, headers={"User-Agent": "Metrovalencia Sync Agent"}, timeout=60)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    # 1. Carga con cabeceras exactas
    routes = load_csv(zf, "routes.txt")
    trips = load_csv(zf, "trips.txt")
    stops = load_csv(zf, "stops.txt")
    stop_times = load_csv(zf, "stop_times.txt")
    
    calendar = load_csv(zf, "calendar.txt") if "calendar.txt" in zf.namelist() else pd.DataFrame()
    calendar_dates = load_csv(zf, "calendar_dates.txt") if "calendar_dates.txt" in zf.namelist() else pd.DataFrame()

    # 2. Generación del diccionario webID: { "stop_id": "stop_name" }
    stops["stop_id_num"] = pd.to_numeric(stops["stop_id"], errors="coerce")
    valid_stops = stops.dropna(subset=["stop_id_num", "stop_name"]).drop_duplicates(subset=["stop_id_num"]).copy()
    valid_stops["stop_id_num"] = valid_stops["stop_id_num"].astype(int)
    valid_stops.sort_values(by="stop_id_num", inplace=True)

    web_id_map = {
        str(row["stop_id_num"]): clean_text(row["stop_name"])
        for _, row in valid_stops.iterrows()
    }
    print(f"Estaciones indexadas en webID: {len(web_id_map)}")

    # 3. Cálculo de terminales (origen y destino) por trip_id
    stop_times["stop_sequence"] = stop_times["stop_sequence"].astype(int)
    stop_times_sorted = stop_times.sort_values(by=["trip_id", "stop_sequence"])

    trip_terminals = stop_times_sorted.groupby("trip_id").agg(
        origin_stop_id=("stop_id", "first"),
        dest_stop_id=("stop_id", "last")
    ).reset_index()

    trip_terminals["origin_stop_id"] = trip_terminals["origin_stop_id"].astype(int)
    trip_terminals["dest_stop_id"] = trip_terminals["dest_stop_id"].astype(int)

    # 4. Mapeo de línea comercial y vehículo
    line_col = "route_short_name" if "route_short_name" in routes.columns else "route_long_name"
    route_map = dict(zip(routes["route_id"], routes[line_col]))
    trips["line"] = trips["route_id"].map(route_map).fillna(trips["route_id"])
    trips["vehiculo"] = trips["service_id"]

    # 5. Detección de servicios activos en la ventana de 2 días
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    trips_by_date = []

    for day_idx, d_str in enumerate(target_dates):
        dt = datetime.strptime(d_str, "%Y%m%d")
        weekday_col = day_names[dt.weekday()]
        active_services = set()

        if not calendar.empty and weekday_col in calendar.columns:
            cal_mask = (
                (calendar["start_date"] <= d_str) & 
                (calendar["end_date"] >= d_str) & 
                (calendar[weekday_col] == "1")
            )
            active_services.update(calendar.loc[cal_mask, "service_id"].tolist())

        if not calendar_dates.empty:
            matches = calendar_dates[calendar_dates["date"] == d_str]
            for _, r in matches.iterrows():
                sid, ex = str(r["service_id"]), str(r["exception_type"])
                if ex == "1":
                    active_services.add(sid)
                elif ex == "2":
                    active_services.discard(sid)

        matched = trips[trips["service_id"].isin(active_services)].copy()
        matched["day_idx"] = day_idx
        trips_by_date.append(matched)

    if not trips_by_date:
        print("Aviso: no hay servicios programados para estas fechas.")
        return

    active_trips_df = pd.concat(trips_by_date, ignore_index=True)
    active_trips_df = active_trips_df.merge(trip_terminals, on="trip_id", how="left")

    # 6. Minutos de paso por parada
    time_col = "departure_time" if "departure_time" in stop_times.columns else "arrival_time"
    stop_times["m"] = stop_times[time_col].apply(time_to_minutes)
    valid_times = stop_times[stop_times["m"] >= 0][["trip_id", "stop_id", "m", "stop_sequence"]].copy()

    merged = valid_times.merge(
        active_trips_df[["trip_id", "line", "origin_stop_id", "dest_stop_id", "vehiculo", "day_idx"]],
        on="trip_id"
    )

    merged.sort_values(by=["day_idx", "m", "stop_sequence"], inplace=True)

    # 7. Salidas compactas agrupadas por estación
    stops_data = {}
    for stop_id, group in merged.groupby("stop_id"):
        stops_data[str(stop_id)] = [
            [
                int(row["day_idx"]),
                int(row["m"]),
                str(row["line"]),
                int(row["origin_stop_id"]),
                int(row["dest_stop_id"]),
                int(row["vehiculo"]) if str(row["vehiculo"]).isdigit() else str(row["vehiculo"])
            ]
            for _, row in group.iterrows()
        ]

    # 8. Guardar JSON optimizado
    compact_payload = {
        "dates": target_dates,
        "webID": web_id_map,
        "stops": stops_data
    }

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(compact_payload, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(output_path) / 1024
    print(f"Generado con éxito: {output_path} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
