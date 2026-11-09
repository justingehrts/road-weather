#!/usr/bin/env python3
"""Run METRo for every site in sites.yaml and save the roadcast as CSV.

Inputs (all produced by earlier steps):
  data/obs/<site>.csv                    RWIS observations (fetch_rwis.py)
  data/forcing/hrrr_<cycle>z_<site>.csv  HRRR forcing, f00..f48 (fetch_hrrr.py)
  sites.yaml                             station type + pavement layers

For each site this writes three METRo input XML files (station, observation,
forecast), runs METRo, and parses its roadcast XML into
  data/roadcast/<cycle>z_<site>.csv

This script runs on the project's Python 3.11 (standard library + PyYAML).
METRo itself is launched as a separate process with the *system* Python,
because METRo needs apt's libxml2 bindings (see scripts/build_metro.sh).

Unit conversions (METRo wants Celsius, km/h, mb, mm, cm, octas):
  * temperatures: K or F -> C
  * wind: HRRR sqrt(u10^2+v10^2) m/s, or OHGO mph -> km/h
  * pressure: Pa -> mb (hPa)
  * cloud cover: percent -> octas (0-8)
  * precipitation: HRRR rate (mm/s) integrated hourly into the running totals
    METRo expects. Each hour's water is counted as snow if HRRR flags snow or
    ice pellets, otherwise as rain (freezing rain counts as rain; METRo itself
    decides it freezes). Snow depth assumes 10 mm of water -> 10 cm of snow.
  * HRRR downward shortwave / longwave radiation are passed straight in as
    METRo's solar flux (sf) and infra-red flux (ir).
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

MISSING = "9999"  # METRo's code for a missing observation value
OBS_WINDOW_HOURS = 12  # METRo wants 3 h minimum, ideally 12 h of observations
# Many OHGO sites have no working subsurface sensor, but METRo insists on a subsurface (40 cm) temperature. Its docs
# allow a pseudo-observation, so a road site with none gets the mean road temperature of the last 24 h (the
# ground at 40 cm changes slowly, so a day's average is a reasonable stand-in).
SUBSURFACE_ESTIMATE_HOURS = 24
STATION_TZ = "EST5EDT"  # Ohio

# Pavement material names -> the four types METRo accepts.
LAYER_ALIASES = {
    "asphalt": "asphalt",
    "cement": "cement",
    "concrete": "cement",
    "crushed rock": "crushed rock",
    "crushed_rock": "crushed rock",
    "gravel": "crushed rock",
    "sand": "sand",
}

ROAD_CONDITION = {
    1: "dry", 2: "wet", 3: "ice_snow", 4: "water_snow_mix",
    5: "dew", 6: "melting_snow", 7: "frost", 8: "icing_rain",
}

ROADCAST_COLUMNS = [
    ("air_temp_c", "at"), ("dewpoint_c", "td"), ("wind_kmh", "ws"),
    ("snow_cm", "sn"), ("rain_mm", "ra"), ("snow_rate", "qp-sn"),
    ("rain_rate", "qp-ra"), ("cloud_octas", "cc"), ("solar_wm2", "sf"),
    ("infrared_wm2", "ir"), ("road_temp_c", "st"), ("subsurface_temp_c", "sst"),
]


class NotEnoughData(Exception):
    """Inputs are fine but too sparse for METRo; the site is skipped, not failed."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """A yellow warning in the GitHub Actions run (plain text elsewhere)."""
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------- helpers
def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def num(value: str):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def metro_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%MZ")


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def sub(parent: ET.Element, tag: str, text) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------- station XML
def build_station_xml(site: dict, path: Path, now: datetime) -> None:
    if site["station_type"] not in ("road", "bridge"):
        raise ValueError(f"station_type must be road or bridge, got {site['station_type']!r}")
    layers = site.get("pavement_layers") or []
    if not layers:
        raise ValueError("no pavement_layers")
    root = ET.Element("station")
    h = ET.SubElement(root, "header")
    sub(h, "filetype", "rwis-configuration")
    sub(h, "version", "1.0")
    sub(h, "road-station", site["id"])
    sub(h, "time-zone", STATION_TZ)
    sub(h, "production-date", metro_time(now))
    c = ET.SubElement(h, "coordinate")
    sub(c, "latitude", site["lat"])
    sub(c, "longitude", site["lon"])
    sub(h, "station-type", site["station_type"])
    ll = ET.SubElement(root, "roadlayer-list")
    for pos, layer in enumerate(layers, start=1):
        material = LAYER_ALIASES.get(str(layer["material"]).strip().lower())
        if material is None:
            raise ValueError(
                f"layer material {layer['material']!r} is not supported by METRo "
                f"(use asphalt, cement/concrete, crushed rock/gravel or sand)")
        rl = ET.SubElement(ll, "roadlayer")
        sub(rl, "position", pos)
        sub(rl, "type", material)
        sub(rl, "thickness", layer["thickness_m"])
    write_xml(root, path)


# ---------------------------------------------------------- forecast XML
def build_forecast_xml(rows: list[dict], cycle: datetime, path: Path) -> None:
    if len(rows) < 2:
        raise ValueError("forecast needs at least 2 hours")
    root = ET.Element("forecast")
    h = ET.SubElement(root, "header")
    sub(h, "version", "1.1")
    sub(h, "production-date", metro_time(cycle))
    pl = ET.SubElement(root, "prediction-list")

    rain_mm = snow_cm = 0.0
    prev = None
    for r in rows:
        v = {k: num(r[k]) for k in (
            "t2m_k", "d2m_k", "u10_ms", "v10_ms", "prate_kgm2s", "tcc_pct",
            "dswrf_wm2", "dlwrf_wm2", "sp_pa", "crain", "cfrzr", "cicep", "csnow")}
        if any(x is None for x in v.values()):
            raise ValueError(f"missing HRRR value at f{r['fxx']}")
        if prev is not None:
            # Trapezoid: average of this and last hour's rate over 3600 s.
            water_mm = max(0.0, (v["prate_kgm2s"] + prev["prate_kgm2s"]) / 2.0 * 3600.0)
            frozen = max(v["csnow"], v["cicep"], prev["csnow"], prev["cicep"]) >= 0.5
            if frozen:
                snow_cm += water_mm  # 10:1 ratio -> 1 mm water = 1 cm snow
            else:
                rain_mm += water_mm
        prev = v
        p = ET.SubElement(pl, "prediction")
        sub(p, "forecast-time", metro_time(parse_utc(r["valid_time_utc"])))
        sub(p, "at", f"{v['t2m_k'] - 273.15:.2f}")
        sub(p, "td", f"{v['d2m_k'] - 273.15:.2f}")
        sub(p, "ws", f"{math.hypot(v['u10_ms'], v['v10_ms']) * 3.6:.1f}")
        sub(p, "ap", f"{v['sp_pa'] / 100.0:.1f}")
        sub(p, "cc", min(8, max(0, round(v["tcc_pct"] / 100.0 * 8))))
        sub(p, "sn", f"{snow_cm:.2f}")
        sub(p, "ra", f"{rain_mm:.2f}")
        sub(p, "sf", f"{max(0.0, v['dswrf_wm2']):.1f}")
        sub(p, "ir", f"{v['dlwrf_wm2']:.1f}")
    write_xml(root, path)


# -------------------------------------------------------- observation XML
def sc_code(text: str) -> str:
    """OHGO surface condition text -> SSI code METRo understands (33 = dry)."""
    t = (text or "").strip().lower()
    if not t:
        return MISSING
    if "dry" in t:
        return "33"
    if any(w in t for w in ("snow", "ice", "icy", "frost", "slush", "slick")):
        return "35"
    return "34"  # wet / damp / chemically wet / anything else = "wet"


def select_observations(rows: list[dict], cycle: datetime, site: dict) -> list[dict]:
    """Pick, convert and sanity-check the observations for one METRo run."""
    parsed = []
    for r in rows:
        try:
            ts = parse_utc(r["obs_time_utc"]).replace(second=0)
        except (KeyError, ValueError):
            continue
        at, td = num(r["air_temp_f"]), num(r["dewpoint_f"])
        ws, st, sst = num(r["wind_avg_mph"]), num(r["road_temp_f"]), num(r["subsurface_temp_f"])
        at_c = None if at is None else f_to_c(at)
        sst_c = None if sst is None else f_to_c(sst)
        if sst_c is None and site["station_type"] == "bridge":
            sst_c = at_c  # METRo's own rule for bridges: air temp stands in
        parsed.append({
            "ts": ts,
            "at": at_c,
            "td": None if td is None else f_to_c(td),
            "ws": None if ws is None else ws * 1.609344,
            "sc": sc_code(r.get("surface_condition", "")),
            "st": None if st is None else f_to_c(st),
            "sst": sst_c,
        })
    if not parsed:
        raise NotEnoughData("no usable observations")
    parsed.sort(key=lambda o: o["ts"])
    latest = parsed[-1]["ts"]
    window = [o for o in parsed if o["ts"] >= latest - timedelta(hours=OBS_WINDOW_HOURS)]
    # Drop duplicate minutes (keep the last).
    window = list({o["ts"]: o for o in window}.values())

    # No subsurface reading at all (no sensor, or it is offline): use the recent mean road temperature.
    if not any(o["sst"] is not None for o in window):
        recent = [o["st"] for o in parsed
                  if o["st"] is not None and o["ts"] > latest - timedelta(hours=SUBSURFACE_ESTIMATE_HOURS)]
        if recent:
            estimate = sum(recent) / len(recent)
            for o in window:
                o["sst"], o["sst_estimated"] = estimate, True

    # METRo's own acceptance rules (see its "Input observation" docs). Checked
    # here so a skipped site gets a plain-English reason, not a METRo traceback.
    with_st = [o for o in window if o["st"] is not None]
    if len(with_st) < 2:
        raise NotEnoughData(f"need at least 2 road-temperature observations in the last "
                         f"{OBS_WINDOW_HOURS} h, have {len(with_st)}")
    both = [o for o in with_st if o["sst"] is not None]
    if not both:
        raise NotEnoughData("no observation has both road and subsurface temperature")
    if not any(abs((b["ts"] - o["ts"]).total_seconds()) <= 4 * 3600
               for b in both for o in with_st if o is not b):
        raise NotEnoughData("no road+subsurface observation within 4 h of another road observation")
    if with_st[-1]["ts"] < cycle:
        raise NotEnoughData(f"newest road-temperature observation ({metro_time(with_st[-1]['ts'])}) "
                         f"is older than the forecast start ({metro_time(cycle)})")
    return window


def build_observation_xml(obs: list[dict], site_id: str, path: Path) -> None:
    root = ET.Element("observation")
    h = ET.SubElement(root, "header")
    sub(h, "filetype", "rwis-observation")
    sub(h, "version", "1.0")
    sub(h, "road-station", site_id)
    ml = ET.SubElement(root, "measure-list")

    def f(v, fmt="{:.2f}"):
        return MISSING if v is None else fmt.format(v)

    for o in obs:
        m = ET.SubElement(ml, "measure")
        sub(m, "observation-time", metro_time(o["ts"]))
        sub(m, "at", f(o["at"]))
        sub(m, "td", f(o["td"]))
        sub(m, "pi", 0)  # OHGO precipitation is not collected yet; assume none
        sub(m, "ws", f(o["ws"], "{:.0f}"))
        sub(m, "sc", o["sc"])
        sub(m, "st", f(o["st"]))
        sub(m, "sst", f(o["sst"]))
    write_xml(root, path)


# --------------------------------------------------------------- run METRo
def metro_env() -> dict:
    """Environment for METRo: system Python, not the 3.11 of this script."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON") and k not in ("LD_LIBRARY_PATH", "VIRTUAL_ENV", "pythonLocation")}
    env["PATH"] = "/usr/bin:/bin:" + env.get("PATH", "")
    return env


def run_metro(metro_dir: Path, python: str, work: Path, roadcast: Path) -> None:
    exe = metro_dir / "usr" / "bin" / "metro"
    cmd = [
        python, str(exe),
        "--input-station", str(work / "station.xml"),
        "--input-observation", str(work / "observation.xml"),
        "--input-forecast", str(work / "forecast.xml"),
        "--output-roadcast", str(roadcast),
        "--use-solarflux-forecast", "--use-infrared-forecast",
        "--log-file", str(work / "metro.log"),
        "--silent",
    ]
    res = subprocess.run(cmd, env=metro_env(), cwd=work, capture_output=True, text=True, timeout=600)
    if res.returncode != 0 or not roadcast.exists():
        tail = (res.stdout + res.stderr).strip().splitlines()[-15:]
        log_tail = []
        if (work / "metro.log").exists():
            log_tail = (work / "metro.log").read_text(errors="replace").strip().splitlines()[-15:]
        raise RuntimeError("METRo failed (exit %s):\n  %s" % (
            res.returncode, "\n  ".join(tail + log_tail)))


# ---------------------------------------------------------- parse roadcast
def roadcast_to_csv(xml_path: Path, csv_path: Path, cycle: datetime) -> int:
    root = ET.parse(xml_path).getroot()
    rows = []
    for p in root.iter("prediction"):
        row = {
            "cycle_utc": cycle.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "roadcast_time_utc": parse_metro_time(p.findtext("roadcast-time")),
        }
        for col, tag in ROADCAST_COLUMNS:
            row[col] = (p.findtext(tag) or "").strip()
        if row["cloud_octas"] == "-1":  # METRo: cloud cover unused (fluxes supplied)
            row["cloud_octas"] = ""
        rc = p.findtext("rc")
        row["road_condition_code"] = (rc or "").strip()
        row["road_condition"] = ROAD_CONDITION.get(int(rc), "unknown") if rc and rc.strip().isdigit() else ""
        rows.append(row)
    if not rows:
        raise RuntimeError("roadcast XML had no predictions")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["cycle_utc", "roadcast_time_utc"] + [c for c, _ in ROADCAST_COLUMNS] + \
           ["road_condition_code", "road_condition"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def parse_metro_time(text: str) -> str:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d)", text or "")
    return (m.group(1) + ":00Z") if m else ""


# -------------------------------------------------------------------- main
def newest_cycle(forcing_dir: Path) -> str | None:
    tags = sorted({m.group(1) for p in forcing_dir.glob("hrrr_*.csv")
                   if (m := re.match(r"hrrr_(\d{10}z)_", p.name))})
    return tags[-1] if tags else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--obs-dir", type=Path, default=Path("data/obs"))
    ap.add_argument("--forcing-dir", type=Path, default=Path("data/forcing"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/roadcast"))
    ap.add_argument("--metro-dir", type=Path, required=True,
                    help="METRo install dir (the folder build_metro.sh created, containing usr/)")
    ap.add_argument("--metro-python", default="/usr/bin/python3",
                    help="system Python that has libxml2 + numpy")
    ap.add_argument("--cycle", help="HRRR cycle tag, e.g. 2026110512z (default: newest in forcing dir)")
    ap.add_argument("--work-dir", type=Path, default=Path(".metro_work"))
    args = ap.parse_args()

    tag = args.cycle or newest_cycle(args.forcing_dir)
    if not tag or not re.fullmatch(r"\d{10}z", tag):
        log("no complete HRRR run found in the forcing folder; skipping METRo")
        return 1
    cycle = datetime.strptime(tag[:-1], "%Y%m%d%H").replace(tzinfo=timezone.utc)
    log(f"HRRR cycle {tag}")

    sites = yaml.safe_load(args.sites.read_text())["sites"]
    written, errors, skipped = 0, 0, 0
    now = datetime.now(timezone.utc)
    for site in sites:
        sid = site["id"]
        forcing = args.forcing_dir / f"hrrr_{tag}_{sid}.csv"
        obs_csv = args.obs_dir / f"{sid}.csv"
        out_csv = args.out_dir / f"{tag}_{sid}.csv"
        work = args.work_dir / sid
        work.mkdir(parents=True, exist_ok=True)
        try:
            if not forcing.exists():
                raise NotEnoughData(f"no forcing file {forcing}")
            if not obs_csv.exists():
                raise NotEnoughData(f"no observations yet ({obs_csv})")
            f_rows = read_csv(forcing)
            obs = select_observations(read_csv(obs_csv), cycle, site)
            build_station_xml(site, work / "station.xml", now)
            build_forecast_xml(f_rows, cycle, work / "forecast.xml")
            build_observation_xml(obs, sid, work / "observation.xml")
        except NotEnoughData as exc:
            skipped += 1
            log(f"{sid}: skipped - {exc}")
            continue
        except Exception as exc:
            errors += 1
            log(f"{sid}: ERROR preparing inputs - {exc}")
            continue
        try:
            if any(o.get("sst_estimated") for o in obs):
                log(f"{sid}: no subsurface sensor reading; using the 24 h mean road temperature "
                    f"({obs[-1]['sst'] * 9 / 5 + 32:.1f} F) as the subsurface estimate")
            roadcast_xml = work / "roadcast.xml"
            roadcast_xml.unlink(missing_ok=True)
            run_metro(args.metro_dir, args.metro_python, work, roadcast_xml)
            n = roadcast_to_csv(roadcast_xml, out_csv, cycle)
            written += 1
            log(f"{sid}: wrote {out_csv} ({n} rows, {len(obs)} observations used)")
        except Exception as exc:
            errors += 1
            log(f"{sid}: ERROR running METRo - {exc}")

    log(f"done: {written} written, {skipped} skipped (not enough data), {errors} errors")
    if written == 0 and errors == 0 and skipped:
        # Expected until observations have been collected: not a failure, but make it visible.
        warn(f"METRo produced no roadcasts: all {skipped} site(s) were skipped for lack of observations or forcing")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
