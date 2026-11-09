#!/usr/bin/env python3
"""List OHGO weather sensor sites (RWIS stations) near Columbus, Ohio.

A one-off helper for filling in sites.yaml. For each site near Columbus it
shows the OHGO `Id` (this is the value that goes in `ohgo_site_id`), where the
site is, and how far it is from downtown Columbus, then prints a ready-to-edit
sites.yaml snippet. It also prints every field name OHGO reports for the
sensors, which shows whether a road surface condition is available at all.

Uses the OHGO_API_KEY environment variable (sent in a header, never printed).
Run by .github/workflows/list-ohgo-sites.yml; results are shown in the run's
summary page and log.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

import requests

API_URL = "https://publicapi.ohgo.com/api/v1/weather-sensor-sites"
COLUMBUS = (39.9612, -83.0007)   # downtown Columbus, Ohio


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def miles_between(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def fetch_sites(api_key: str) -> list[dict]:
    resp = requests.get(API_URL, headers={"Authorization": f"APIKEY {api_key}"},
                        params={"page-all": "true"}, timeout=60)
    if resp.status_code in (401, 403):
        raise SystemExit("OHGO rejected the API key (HTTP %d). Check that the OHGO_API_KEY secret holds the "
                         "key exactly, with no extra spaces." % resp.status_code)
    resp.raise_for_status()
    return resp.json().get("Results", [])


def nearby(sites: list[dict], center: tuple[float, float], radius_miles: float) -> list[dict]:
    """Sites within the radius, nearest first, each with a `miles` distance added."""
    out = []
    for s in sites:
        lat, lon = s.get("Latitude"), s.get("Longitude")
        if lat is None or lon is None:
            continue
        d = miles_between(center[0], center[1], float(lat), float(lon))
        if d <= radius_miles:
            out.append({**s, "miles": d})
    return sorted(out, key=lambda s: s["miles"])


def guess_station_type(site: dict) -> str:
    text = f"{site.get('Location', '')} {site.get('Description', '')}".lower()
    return "bridge" if "bridge" in text or "overpass" in text else "road"


def sensor_fields(sites: list[dict]) -> dict[str, list[str]]:
    """Every field name seen on each kind of sensor, across all the sites."""
    seen: dict[str, set[str]] = {"AtmosphericSensors": set(), "SurfaceSensors": set()}
    for s in sites:
        for kind in seen:
            for sensor in s.get(kind) or []:
                seen[kind].update(sensor.keys())
    return {k: sorted(v) for k, v in seen.items()}


def yaml_snippet(sites: list[dict]) -> str:
    lines = ["sites:"]
    for s in sites:
        name = (s.get("Location") or s.get("Description") or f"OHGO site {s['Id']}").replace('"', "'")
        lines += [
            f"  - id: rwis_{re.sub(r'[^A-Za-z0-9]+', '_', str(s['Id'])).strip('_').lower()}   # your own short name",
            f'    name: "{name}"',
            f"    lat: {float(s['Latitude']):.5f}",
            f"    lon: {float(s['Longitude']):.5f}",
            f'    ohgo_site_id: "{s["Id"]}"',
            f"    station_type: {guess_station_type(s)}   # CHECK: road or bridge?",
            "    pavement_layers:   # TODO: real layers, top to bottom",
            "      - { material: asphalt,  thickness_m: 0.10 }",
            "      - { material: crushed rock, thickness_m: 0.30 }",
            "",
        ]
    return "\n".join(lines)


def report(all_sites: list[dict], near: list[dict], radius: float) -> str:
    md = [f"# OHGO weather sensor sites near Columbus",
          "",
          f"OHGO lists **{len(all_sites)}** sites statewide; **{len(near)}** are within **{radius:g} miles** of downtown Columbus.",
          "",
          "## Sites (nearest first)",
          "",
          "The **Id** column is the value that goes in `ohgo_site_id` in `sites.yaml`.",
          "",
          "| Id | Location | Description | Miles from downtown | Lat | Lon | Air temp F now |",
          "|---|---|---|---|---|---|---|"]
    for s in near:
        md.append(f"| `{s['Id']}` | {s.get('Location') or ''} | {s.get('Description') or ''} | {s['miles']:.1f} | "
                  f"{float(s['Latitude']):.4f} | {float(s['Longitude']):.4f} | {s.get('AverageAirTemperature') or ''} |")
    md += ["", "## Starting point for sites.yaml", "",
           "Copy the sites you want. Edit the names, check each station type, and replace the pavement layers "
           "with real ones.", "", "```yaml", yaml_snippet(near), "```", ""]

    fields = sensor_fields(all_sites)
    md += ["## What OHGO reports for each sensor", "",
           "Look for anything about road surface *condition* (dry, wet, frost, ice...). Tell Claude what you find.", ""]
    for kind, names in fields.items():
        md.append(f"- **{kind}**: {', '.join(f'`{n}`' for n in names) or '(none)'}")

    if near:
        sample = {k: v for k, v in near[0].items() if k != "miles"}
        md += ["", f"## One complete record (site `{near[0]['Id']}`)", "", "```json",
               json.dumps(sample, indent=2), "```", ""]
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--radius-miles", type=float, default=40.0)
    ap.add_argument("--lat", type=float, default=COLUMBUS[0])
    ap.add_argument("--lon", type=float, default=COLUMBUS[1])
    args = ap.parse_args()

    key = os.environ.get("OHGO_API_KEY", "").strip()
    if not key:
        log("OHGO_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.")
        return 1

    sites = fetch_sites(key)
    near = nearby(sites, (args.lat, args.lon), args.radius_miles)
    text = report(sites, near, args.radius_miles)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    if not near:
        log(f"No sites within {args.radius_miles:g} miles; try a bigger radius.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
