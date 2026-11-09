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

from ohgo_api import normalize_keys

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


# Ways of asking OHGO for the sites. They are tried in order until one returns something,
# and every attempt is reported, so an empty answer can be diagnosed.
ATTEMPTS = [
    ("all pages", {"page-all": "true"}),
    ("default paging", {}),
    ("region columbus", {"region": "columbus"}),
    ("region central-ohio", {"region": "central-ohio"}),
]


def fetch_once(api_key: str, params: dict) -> dict:
    """One request. Returns what came back, without hiding anything that looks odd."""
    resp = requests.get(API_URL, headers={"Authorization": f"APIKEY {api_key}"}, params=params, timeout=60)
    if resp.status_code in (401, 403):
        raise SystemExit("OHGO rejected the API key (HTTP %d). Check that the OHGO_API_KEY secret holds the "
                         "key exactly, with no extra spaces." % resp.status_code)
    out = {"status": resp.status_code, "results": [], "keys": [], "meta": {}, "preview": resp.text[:600]}
    if resp.status_code != 200:
        return out
    try:
        body = resp.json()
    except ValueError:
        return out
    if isinstance(body, dict):
        out["keys"] = sorted(body.keys())             # as OHGO spelled them (shows camelCase vs PascalCase)
        body = normalize_keys(body)                   # the rest of the code uses PascalCase
        out["results"] = body.get("Results") or []
        out["meta"] = {k: body[k] for k in ("TotalResultCount", "CurrentResultCount", "TotalPageCount",
                                             "AccecptedFilters", "AcceptedFilters", "RejectedFilters") if k in body}
    elif isinstance(body, list):
        out["keys"] = ["(a bare list)"]
        out["results"] = normalize_keys(body)
    return out


def fetch_sites(api_key: str) -> tuple[list[dict], list[dict]]:
    """Return (sites, attempts). `attempts` describes every request made, for the report."""
    attempts, sites = [], []
    for label, params in ATTEMPTS:
        r = fetch_once(api_key, params)
        attempts.append({"label": label, "params": params, **{k: r[k] for k in ("status", "keys", "meta", "preview")},
                         "count": len(r["results"])})
        if r["results"]:
            sites = r["results"]
            break
    return sites, attempts


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


def pavement_sensors(site: dict) -> list[dict]:
    return [x for x in (site.get("SurfaceSensors") or []) if isinstance(x, dict)]


def valid_reading(value, lo=-50, hi=180) -> bool:
    """OHGO fills empty sensor slots with -9999999 (and other garbage); a real temperature is plausible."""
    return isinstance(value, (int, float)) and lo <= value <= hi


def working_pavement_sensors(site: dict) -> list[dict]:
    """Surface sensors that are reporting a real pavement temperature right now."""
    return [x for x in pavement_sensors(site) if valid_reading(x.get("SurfaceTemperature"))]


def pavement_temp(site: dict):
    """First real pavement temperature (F), or None if no pavement sensor is reporting."""
    for x in working_pavement_sensors(site):
        return x["SurfaceTemperature"]
    return None


def subsurface_temp(site: dict):
    for x in pavement_sensors(site):
        if valid_reading(x.get("SubSurfaceTemperature")):
            return x["SubSurfaceTemperature"]
    return None


def compact_sample(site: dict) -> dict:
    """A copy of a site record for display, with repeated empty sensor slots collapsed."""
    out = {k: v for k, v in site.items() if k != "miles"}
    sensors = pavement_sensors(site)
    shown = [x for x in sensors if valid_reading(x.get("SurfaceTemperature")) or valid_reading(x.get("SubSurfaceTemperature"))][:2]
    empty = [x for x in sensors if x not in shown]
    if empty:
        shown = shown + empty[:1]
        out["SurfaceSensors"] = shown
        out["SurfaceSensorsNote"] = (f"{len(sensors)} surface sensor slots in total; {len(empty)} report no data "
                                     f"(-9999999); only {len(shown)} are shown here")
    return out


def guess_station_type(site: dict) -> str:
    """OHGO does not say whether a sensor is on a bridge deck. Only the word "bridge" in the name is a hint.

    "Overpass" is deliberately NOT treated as a bridge: it usually means a cross street or railroad passes
    over the site's road, so the sensor is on ordinary pavement beneath it (checked against the national
    bridge inventory for site 200015 on 2026-11-09).
    """
    text = f"{site.get('Location', '')} {site.get('Description', '')}".lower()
    return "bridge" if "bridge" in text else "road"


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
            f"    station_type: {guess_station_type(s)}   # CHECK: OHGO does not say; a bridge-deck sensor is rare",
            "    pavement_layers:   # TODO: real layers, top to bottom",
            "      - { material: asphalt,  thickness_m: 0.10 }",
            "      - { material: crushed rock, thickness_m: 0.30 }",
            "",
        ]
    return "\n".join(lines)


def attempts_report(attempts: list[dict]) -> list[str]:
    """How OHGO answered each request. Raw detail is shown only for requests that returned nothing."""
    md = ["## What OHGO answered", "", "| Request | HTTP status | Sites returned | Fields in the answer |", "|---|---|---|---|"]
    for a in attempts:
        md.append(f"| {a['label']} | {a['status']} | {a['count']} | {', '.join(f'`{k}`' for k in a['keys']) or '(none)'} |")
    for a in attempts:
        if not a["count"]:
            md += ["", f"**{a['label']}** returned no sites. OHGO's own counts and filter notes:", "", "```json",
                   json.dumps(a["meta"], indent=2) if a["meta"] else "(none)", "```",
                   "", "Start of the raw answer:", "", "```", a["preview"], "```"]
    return md + [""]


def report(all_sites: list[dict], near: list[dict], radius: float, attempts: list[dict] | None = None) -> str:
    md = [f"# OHGO weather sensor sites near Columbus",
          "",
          f"OHGO lists **{len(all_sites)}** sites statewide; **{len(near)}** are within **{radius:g} miles** of downtown Columbus.",
          "",
          "## Sites (nearest first)",
          "",
          "The **Id** column is the value that goes in `ohgo_site_id` in `sites.yaml` (it is also the last part of the "
          "site's address in OHGO's API, e.g. `.../weather-sensor-sites/200110`).",
          "",
          "OHGO's **Location** text is an ODOT code, roughly: district, station code, county, route and milepost, "
          "then the cross street. For example `D05 OH101FS LIC IR70 112.16- IR70 @ SR256` is District 5, county LIC "
          "(Licking), Interstate 70 at milepost 112.16, at State Route 256.",
          "",
          "| Id | Location | Miles from downtown | Pavement sensors working now | Pavement temp F | Subsurface temp F | Air temp F |",
          "|---|---|---|---|---|---|---|"]
    for s in near:
        pt, st = pavement_temp(s), subsurface_temp(s)
        total, working = len(pavement_sensors(s)), len(working_pavement_sensors(s))
        label = f"{working} of {total}" if total else "**no sensors**"
        if total and not working:
            label = f"**0** of {total}"
        md.append(f"| `{s['Id']}` | {s.get('Location') or ''} | {s['miles']:.1f} | {label} | "
                  f"{'' if pt is None else pt} | {'' if st is None else st} | {s.get('AverageAirTemperature') or ''} |")
    usable = [s for s in near if working_pavement_sensors(s)]
    skipped = [s for s in near if not working_pavement_sensors(s)]
    md += ["", f"**{len(usable)} of these {len(near)} sites have a pavement sensor reporting a real temperature right now.** "
               "METRo needs road surface temperature, so sites with none cannot be used. OHGO lists empty sensor "
               "slots with the value -9999999, so a site can show several sensors and still report nothing. "
               "Those sites are left out of the sites.yaml snippet below. A sensor can be down for a while, "
               "so run this again another day before ruling a site out."]
    md += ["", "## Starting point for sites.yaml", "",
           "Copy the sites you want. Edit the names, check each station type, and replace the pavement layers "
           "with real ones.", "", "```yaml", yaml_snippet(usable), "```", ""]
    if skipped:
        md += ["Left out (no pavement reading right now): " + ", ".join(f"`{x['Id']}`" for x in skipped), ""]

    fields = sensor_fields(all_sites)
    md += ["## What OHGO reports for each sensor", "",
           "Look for anything about road surface *condition* (dry, wet, frost, ice...). Tell Claude what you find.", ""]
    for kind, names in fields.items():
        md.append(f"- **{kind}**: {', '.join(f'`{n}`' for n in names) or '(none)'}")

    if near:
        pick = next((x for x in near if working_pavement_sensors(x)), near[0])
        sample = compact_sample(pick)
        md += ["", f"## One site's record (site `{pick['Id']}`" + ("" if working_pavement_sensors(pick) else ", no working pavement sensor") + ")", "", "```json",
               json.dumps(sample, indent=2), "```", ""]
    if attempts:
        md += [""] + attempts_report(attempts)
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

    sites, attempts = fetch_sites(key)
    near = nearby(sites, (args.lat, args.lon), args.radius_miles)
    text = report(sites, near, args.radius_miles, attempts)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    if not sites:
        log("OHGO answered but returned no sites at all; see the 'What OHGO answered' section.")
    elif not near:
        log(f"No sites within {args.radius_miles:g} miles; try a bigger radius.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
