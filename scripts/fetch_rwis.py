#!/usr/bin/env python3
"""Fetch RWIS observations from the OHGO public API and append them to CSVs.

Calls GET /api/v1/weather-sensor-sites, keeps only the sites listed in
sites.yaml (matched on ohgo_site_id), and appends one row per site to
<out-dir>/<site id>.csv. Timestamps are stored in UTC. Values stay in the
units OHGO reports (Fahrenheit, mph); convert later when feeding METRo.

The API key is read from the OHGO_API_KEY environment variable and sent in
the Authorization header, so it never appears in a URL or log.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

API_URL = "https://publicapi.ohgo.com/api/v1/weather-sensor-sites"
# Only used if OHGO sends a timestamp with no timezone. ASSUMPTION: Ohio local time.
NAIVE_TZ = ZoneInfo("America/New_York")

COLUMNS = [
    "obs_time_utc",
    "air_temp_f",
    "dewpoint_f",
    "wind_avg_mph",
    "wind_max_mph",
    "wind_dir",
    "road_temp_f",
    "subsurface_temp_f",
    "surface_condition",
    "surface_sensor_status",
]

# Plausible physical ranges; anything outside is treated as a sensor error.
RANGES = {
    "air_temp_f": (-50.0, 130.0),
    "dewpoint_f": (-60.0, 100.0),
    "wind_avg_mph": (0.0, 150.0),
    "wind_max_mph": (0.0, 200.0),
    "road_temp_f": (-40.0, 180.0),
    "subsurface_temp_f": (-40.0, 150.0),
}
# Keys that might hold the road surface condition (dry/wet/snow/ice...).
# The OHGO docs do not list one, so we look for a few likely names.
CONDITION_KEYS = ("SurfaceCondition", "RoadCondition", "SurfaceStatus")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def to_float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def valid(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    lo, hi = RANGES[name]
    if lo <= value <= hi:
        return value
    log(f"  dropping out-of-range {name}={value}")
    return None


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NAIVE_TZ)
    return dt.astimezone(timezone.utc)


def load_sites(path: Path) -> dict[str, str]:
    """Return {ohgo_site_id: our site id}, skipping placeholder entries."""
    sites = yaml.safe_load(path.read_text())["sites"]
    mapping: dict[str, str] = {}
    for s in sites:
        ohgo_id = str(s.get("ohgo_site_id", "")).strip()
        if not ohgo_id or ohgo_id.upper() == "TODO":
            log(f"skipping {s['id']}: ohgo_site_id not filled in yet")
            continue
        mapping[ohgo_id] = s["id"]
    return mapping


def fetch_all(api_key: str) -> list[dict]:
    resp = requests.get(
        API_URL,
        headers={"Authorization": f"APIKEY {api_key}"},
        params={"page-all": "true"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("Results", [])


def first_valid(sensors: list[dict], key: str, name: str):
    for s in sensors or []:
        v = valid(name, to_float(s.get(key)))
        if v is not None:
            return v, s
    return None, None


def build_row(site: dict) -> dict | None:
    atmos = site.get("AtmosphericSensors") or []
    surf = site.get("SurfaceSensors") or []

    air, air_s = first_valid(atmos, "AirTemperature", "air_temp_f")
    dew, _ = first_valid(atmos, "DewpointTemperature", "dewpoint_f")
    # Dewpoint above air temp by a wide margin means a bad reading.
    if dew is not None and air is not None and dew > air + 2.0:
        log(f"  dropping dewpoint {dew} (above air temp {air})")
        dew = None
    wavg, _ = first_valid(atmos, "AverageWindSpeed", "wind_avg_mph")
    wmax, _ = first_valid(atmos, "MaximumWindSpeed", "wind_max_mph")
    road, road_s = first_valid(surf, "SurfaceTemperature", "road_temp_f")
    sub, _ = first_valid(surf, "SubSurfaceTemperature", "subsurface_temp_f")

    wind_dir = next((str(s["WindDirection"]) for s in atmos if s.get("WindDirection")), "")
    cond, status = "", ""
    for s in surf:
        for k in CONDITION_KEYS:
            if s.get(k):
                cond = str(s[k])
                break
        if s.get("Status") and not status:
            status = str(s["Status"])
        if cond:
            break

    measurements = [air, dew, wavg, wmax, road, sub]
    if all(m is None for m in measurements):
        return None

    # Row time = newest LastUpdate among the sensors we actually used.
    used = [s for s in (air_s, road_s) if s]
    times = [t for t in (parse_time(s.get("LastUpdate")) for s in used) if t]
    if not times:
        times = [t for t in (parse_time(s.get("LastUpdate")) for s in atmos + surf) if t]
    if not times:
        return None
    ts = max(times)

    def fmt(v):
        return "" if v is None else f"{v:g}"

    return {
        "obs_time_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "air_temp_f": fmt(air),
        "dewpoint_f": fmt(dew),
        "wind_avg_mph": fmt(wavg),
        "wind_max_mph": fmt(wmax),
        "wind_dir": wind_dir,
        "road_temp_f": fmt(road),
        "subsurface_temp_f": fmt(sub),
        "surface_condition": cond,
        "surface_sensor_status": status,
    }


def append_row(csv_path: Path, row: dict) -> bool:
    """Append row unless its timestamp is already in the file."""
    existing: set[str] = set()
    if csv_path.exists():
        with csv_path.open(newline="") as f:
            existing = {r["obs_time_utc"] for r in csv.DictReader(f)}
    if row["obs_time_utc"] in existing:
        return False
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/obs"))
    args = ap.parse_args()

    api_key = os.environ.get("OHGO_API_KEY")
    if not api_key:
        log("OHGO_API_KEY is not set")
        return 1

    wanted = load_sites(args.sites)
    if not wanted:
        log("no sites with a real ohgo_site_id in sites.yaml; nothing to do")
        return 0

    results = fetch_all(api_key)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    seen, added = set(), 0
    for site in results:
        ohgo_id = str(site.get("Id"))
        if ohgo_id not in wanted:
            continue
        seen.add(ohgo_id)
        name = wanted[ohgo_id]
        row = build_row(site)
        if row is None:
            log(f"{name}: no usable observation")
            continue
        if append_row(args.out_dir / f"{name}.csv", row):
            added += 1
            log(f"{name}: added {row['obs_time_utc']}")
        else:
            log(f"{name}: {row['obs_time_utc']} already recorded")

    for ohgo_id in sorted(set(wanted) - seen):
        log(f"warning: {wanted[ohgo_id]} (ohgo_site_id {ohgo_id}) not found in OHGO response")
    log(f"done: {added} new row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
