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
    if pd.isna(time_str):
        return -1
    parts = str(time_str).strip().split(":")
    if len(parts) >= 2:
        return int(parts[0]) * 60 + int(parts[1])
    return -1

def clean_text(val: str) -> str:
    if pd.isna(val):
        return ""
    normalized = unicodedata.normalize("NFKC", str(val))
    return " ".join(normalized.split())

def main():
    tz = ZoneInfo("Europe/Madrid")
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(2)]
    print(f"Generando previsión compacta para: {target_dates}")

    resp = requests.get(GTFS_URL, headers={"User-Agent": "Metrovalencia Sync"}, timeout=60)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    routes = pd.read_csv(zf.open("routes.txt"), dtype=str)
    trips = pd.read_csv(zf.open("trips.txt"), dtype=str)
    stop_times = pd.read_csv(zf.open("stop_times.txt"), dtype=str)
    
    calendar = pd.read_csv(zf.open("calendar.txt"), dtype=str) if "calendar.txt" in zf.namelist() else pd.DataFrame()
    calendar_dates = pd.read_csv(zf.open("calendar_dates.txt"), dtype=str) if "calendar_dates.txt" in zf.namelist() else pd.DataFrame()

    line_col = "route_short_name" if "route_short_name" in routes.columns else "route_long_name"
    route_map = dict(zip(routes["route_id"], routes[line_col]))
    trips["line"] = trips["route_id"].map(route_map).fillna(trips["route_id"])

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
        matched["vehiculo"] = matched["service_id"]
        trips_by_date.append(matched)

    if not trips_by_date:
        print("Sin servicios activos.")
        return

    active_trips_df = pd.concat(trips_by_date, ignore_index=True)

    # Limpiar y mapear destinos en un índice global
    active_trips_df["dest_clean"] = active_trips_df["trip_headsign"].apply(clean_text)
    unique_dests = sorted(active_trips_df["dest_clean"].unique().tolist())
    dest_map = {name: idx for idx, name in enumerate(unique_dests)}
    active_trips_df["dest_idx"] = active_trips_df["dest_clean"].map(dest_map)

    # Stop times
    time_col = "departure_time" if "departure_time" in stop_times.columns else "arrival_time"
    stop_times["m"] = stop_times[time_col].apply(time_to_minutes)
    valid_times = stop_times[stop_times["m"] >= 0][["trip_id", "stop_id", "m", "stop_sequence"]].copy()

    merged = valid_times.merge(
        active_trips_df[["trip_id", "line", "dest_idx", "vehiculo", "day_idx"]],
        on="trip_id"
    )

    merged["stop_sequence"] = merged["stop_sequence"].astype(int)
    merged.sort_values(by=["day_idx", "m", "stop_sequence"], inplace=True)

    # Generar salida con tuplas compactas
    stops_data = {}
    for stop_id, group in merged.groupby("stop_id"):
        # [day_idx, m, line, dest_idx, trip_id, vehiculo]
        stops_data[str(stop_id)] = [
            [
                int(row["day_idx"]),
                int(row["m"]),
                str(row["line"]),
                int(row["dest_idx"]),
                str(row["trip_id"]),
                str(row["vehiculo"])
            ]
            for _, row in group.iterrows()
        ]

    compact_payload = {
        "dates": target_dates,
        "destinations": unique_dests,
        "stops": stops_data
    }

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        # Separadores compactos sin espacios
        json.dump(compact_payload, f, ensure_ascii=False, separators=(",", ":"))

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Archivo generado: {output_path} ({size_mb:.2f} MB)")

if __name__ == "__main__":
    main()
