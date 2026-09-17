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
    """Convierte 'HH:MM:SS' a minutos del día operativo (24:01 -> 1441)."""
    if pd.isna(time_str):
        return -1
    parts = str(time_str).strip().split(":")
    if len(parts) >= 2:
        return int(parts[0]) * 60 + int(parts[1])
    return -1

def clean_text(val: str) -> str:
    """Normaliza cadenas y elimina dobles espacios."""
    if pd.isna(val):
        return ""
    normalized = unicodedata.normalize("NFKC", str(val))
    return " ".join(normalized.split())

def main():
    # 1. Definir ventana de 2 días en hora peninsular
    tz = ZoneInfo("Europe/Madrid")
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(2)]
    print(f"Calculando previsiones para los días: {target_dates}")

    # 2. Descarga del GTFS
    print("Descargando GTFS de Transitous...")
    headers = {"User-Agent": "Mozilla/5.0 (Metrovalencia 2Day Sync Agent)"}
    resp = requests.get(GTFS_URL, headers=headers, timeout=60)
    resp.raise_for_status()

    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    # 3. Carga selectiva de tablas
    routes = pd.read_csv(zf.open("routes.txt"), dtype=str)
    trips = pd.read_csv(zf.open("trips.txt"), dtype=str)
    stop_times = pd.read_csv(zf.open("stop_times.txt"), dtype=str)
    
    calendar = pd.read_csv(zf.open("calendar.txt"), dtype=str) if "calendar.txt" in zf.namelist() else pd.DataFrame()
    calendar_dates = pd.read_csv(zf.open("calendar_dates.txt"), dtype=str) if "calendar_dates.txt" in zf.namelist() else pd.DataFrame()

    # Mapeo de línea: route_id -> route_short_name (1, 2, 3...)
    line_col = "route_short_name" if "route_short_name" in routes.columns else "route_long_name"
    route_map = dict(zip(routes["route_id"], routes[line_col]))
    trips["line"] = trips["route_id"].map(route_map).fillna(trips["route_id"])

    # 4. Resolver servicios activos para cada uno de los 2 días
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active_services_by_date = {}

    for d_str in target_dates:
        dt = datetime.strptime(d_str, "%Y%m%d")
        weekday_col = day_names[dt.weekday()]
        active_services = set()

        # Evaluación sobre calendar.txt
        if not calendar.empty:
            cal_mask = (
                (calendar["start_date"] <= d_str) & 
                (calendar["end_date"] >= d_str) & 
                (calendar[weekday_col] == "1")
            )
            active_services.update(calendar.loc[cal_mask, "service_id"].tolist())

        # Excepciones sobre calendar_dates.txt
        if not calendar_dates.empty:
            date_matches = calendar_dates[calendar_dates["date"] == d_str]
            for _, r in date_matches.iterrows():
                sid = str(r["service_id"])
                ex = str(r["exception_type"])
                if ex == "1":
                    active_services.add(sid)
                elif ex == "2":
                    active_services.discard(sid)

        active_services_by_date[d_str] = active_services

    # 5. Filtrar trips por fecha y transformar service_id en "vehiculo"
    trips_by_date = []
    for d_str, services in active_services_by_date.items():
        matched = trips[trips["service_id"].isin(services)].copy()
        matched["date"] = d_str
        matched["vehiculo"] = matched["service_id"]
        trips_by_date.append(matched)

    if not trips_by_date:
        print("No se encontraron servicios para las fechas indicadas.")
        return

    active_trips_df = pd.concat(trips_by_date, ignore_index=True)

    # 6. Cruce con stop_times
    time_col = "departure_time" if "departure_time" in stop_times.columns else "arrival_time"
    stop_times["m"] = stop_times[time_col].apply(time_to_minutes)
    valid_times = stop_times[stop_times["m"] >= 0][["trip_id", "stop_id", "m", "stop_sequence"]].copy()

    # Join para obtener estación, hora, línea, destino, vehiculo y trip_id
    merged = valid_times.merge(
        active_trips_df[["trip_id", "line", "trip_headsign", "vehiculo", "date"]],
        on="trip_id"
    )

    # Ordenar cronológicamente por día y minuto de paso
    merged["stop_sequence"] = merged["stop_sequence"].astype(int)
    merged.sort_values(by=["date", "m", "stop_sequence"], inplace=True)

    # 7. Consolidación en diccionario único agrupado por stop_id
    schedule_data = {}
    for stop_id, group in merged.groupby("stop_id"):
        passages = []
        for _, row in group.iterrows():
            passages.append({
                "date": row["date"],
                "m": int(row["m"]),
                "line": str(row["line"]),
                "dest": clean_text(row["trip_headsign"]),
                "trip_id": str(row["trip_id"]),
                "vehiculo": str(row["vehiculo"])
            })
        schedule_data[str(stop_id)] = passages

    # 8. Guardar archivo final
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(schedule_data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Completado. Generado {output_path} con {len(schedule_data)} estaciones.")

if __name__ == "__main__":
    main()
