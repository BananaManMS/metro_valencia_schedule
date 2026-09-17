import io
import os
import json
import shutil
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests

GTFS_URL = "https://api.transitous.org/gtfs/es_Metro-de-Valencia.gtfs.zip"
OUTPUT_DIR = "data"
OUTPUT_FILE = "metro_schedule.json"

def time_to_minutes(time_str: str) -> int:
    if pd.isna(time_str):
        return -1
    parts = str(time_str).strip().split(":")
    if len(parts) >= 2:
        return int(parts[0]) * 60 + int(parts[1])
    return -1

def main():
    tz = ZoneInfo("Europe/Madrid")
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(2)]
    print(f"Generando previsiones de origen/término para: {target_dates}")

    resp = requests.get(GTFS_URL, headers={"User-Agent": "Metrovalencia Sync"}, timeout=60)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    routes = pd.read_csv(zf.open("routes.txt"), dtype=str)
    trips = pd.read_csv(zf.open("trips.txt"), dtype=str)
    stop_times = pd.read_csv(zf.open("stop_times.txt"), dtype=str)
    
    calendar = pd.read_csv(zf.open("calendar.txt"), dtype=str) if "calendar.txt" in zf.namelist() else pd.DataFrame()
    calendar_dates = pd.read_csv(zf.open("calendar_dates.txt"), dtype=str) if "calendar_dates.txt" in zf.namelist() else pd.DataFrame()

    # 1. Precalcular origen y término por trip_id
    print("Calculando estaciones cabecera (origen y destino) por viaje...")
    stop_times["stop_sequence"] = stop_times["stop_sequence"].astype(int)
    stop_times_sorted = stop_times.sort_values(by=["trip_id", "stop_sequence"])

    trip_terminals = stop_times_sorted.groupby("trip_id").agg(
        origin_stop_id=("stop_id", "first"),
        dest_stop_id=("stop_id", "last")
    ).reset_index()

    # Convertir stop_ids a int para menor huella JSON
    trip_terminals["origin_stop_id"] = trip_terminals["origin_stop_id"].astype(int)
    trip_terminals["dest_stop_id"] = trip_terminals["dest_stop_id"].astype(int)

    # 2. Mapeo de líneas
    line_col = "route_short_name" if "route_short_name" in routes.columns else "route_long_name"
    route_map = dict(zip(routes["route_id"], routes[line_col]))
    trips["line"] = trips["route_id"].map(route_map).fillna(trips["route_id"])

    # 3. Servicios activos en los 2 días
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    trips_by_date = []

    for day_idx, d_str in enumerate(target_dates):
        dt = datetime.strptime(d_str, "%Y%m%d")
        weekday_col = day_names[dt.weekday()]
        active_services = set()

        if not calendar.empty:
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
        print("Sin servicios activos.")
        return

    active_trips_df = pd.concat(trips_by_date, ignore_index=True)

    # Inyectar origen y destino precalculados a los viajes activos
    active_trips_df = active_trips_df.merge(trip_terminals, on="trip_id", how="left")

    # 4. Stop times con minutos de paso
    time_col = "departure_time" if "departure_time" in stop_times.columns else "arrival_time"
    stop_times["m"] = stop_times[time_col].apply(time_to_minutes)
    valid_times = stop_times[stop_times["m"] >= 0][["trip_id", "stop_id", "m", "stop_sequence"]].copy()

    merged = valid_times.merge(
        active_trips_df[["trip_id", "line", "origin_stop_id", "dest_stop_id", "day_idx"]],
        on="trip_id"
    )

    merged.sort_values(by=["day_idx", "m", "stop_sequence"], inplace=True)

    # 5. Generar payload sin claves redundantes
    stops_data = {}
    for stop_id, group in merged.groupby("stop_id"):
        # [day_idx, m, line, origin_stop_id, dest_stop_id]
        stops_data[str(stop_id)] = [
            [
                int(row["day_idx"]),
                int(row["m"]),
                str(row["line"]),
                int(row["origin_stop_id"]),
                int(row["dest_stop_id"])
            ]
            for _, row in group.iterrows()
        ]

    compact_payload = {
        "dates": target_dates,
        "stops": stops_data
    }

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(compact_payload, f, separators=(",", ":"))

    size_kb = os.path.getsize(output_path) / 1024
    print(f"Generado con éxito: {output_path} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
