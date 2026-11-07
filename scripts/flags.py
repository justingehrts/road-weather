#!/usr/bin/env python3
"""Apply the hazard-flag rules in docs/flags.md to each site's METRo roadcast.

Reads   data/roadcast/<cycle>_<site>.csv   (run_metro.py)
        data/forcing/hrrr_<cycle>_<site>.csv (fetch_hrrr.py; cloud cover and
                                              freezing-rain flag)
        sites.yaml                          (station type, bridge pairing, traffic)
Writes  data/flags/<cycle>.json             (flag intervals per site)
        data/latest.json                    (pointer the Streamlit app reads)

Every rule is evaluated at each roadcast step (METRo: 20 minutes). A flag is
"active" at the steps where its condition holds; consecutive active steps are
merged into one interval. All thresholds are in THRESHOLDS below, are starting
guesses (see docs/flags.md), and are copied into the output file.

Runs on the project's Python 3.11 with the standard library + PyYAML.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# ---- Starting-point thresholds (units: degrees F, mph, inches/h, hours) ----
THRESHOLDS = {
    "freezing_f": 32.0,
    # Pavement frost
    "frost_min_hours": 2.0,
    "frost_clear_calm_margin_f": 3.0,   # extra dewpoint-over-RST needed on clear, calm nights
    "clear_cloud_pct": 25.0,            # HRRR total cloud cover below this = "clear"
    "calm_wind_mph": 5.0,               # wind below this = "calm"
    # Black ice / refreeze
    "wet_water_mm": 0.0,                # METRo liquid water on road above this = "wet"
    # Flash freeze
    "flash_drop_f": 6.0,
    "flash_window_h": 3.0,
    # Wheel-track glazing
    "glaze_rst_f": 20.0,
    "glaze_light_snow_in_per_h": 0.04,  # liquid-equivalent
    "glaze_blowing_wind_mph": 15.0,
    "fresh_snow_hours": 6.0,            # snow fell within this many hours = "fresh"
    "snow_to_water_ratio": 10.0,        # 10 cm of snow = 1 cm (10 mm) of water
    # Salt struggles
    "salt_rst_f": 15.0,
    "snow_ice_codes": [3, 4, 7, 8],     # METRo rc: ice/snow, water-snow mix, frost, icing rain
    # Freezing rain
    "freezing_rain_prob": 0.5,
}

FLAG_NAMES = {
    "pavement_frost": "Pavement frost",
    "black_ice": "Black ice / refreeze",
    "flash_freeze": "Flash freeze",
    "wheel_track_glazing": "Wheel-track glazing",
    "bridges_first": "Bridges first",
    "salt_struggles": "Salt struggles",
    "freezing_rain": "Freezing rain",
}

UNITS = {"temperature": "F", "wind": "mph", "score_frost": "F*h"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ loading
def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def c_to_f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


class Roadcast:
    """One site's roadcast, converted to degrees F / mph."""

    def __init__(self, rows: list[dict]):
        rows = sorted(rows, key=lambda r: r["roadcast_time_utc"])
        self.t = [parse_time(r["roadcast_time_utc"]) for r in rows]
        self.rst = [c_to_f(num(r["road_temp_c"])) for r in rows]
        self.dp = [c_to_f(num(r["dewpoint_c"])) for r in rows]
        self.air = [c_to_f(num(r["air_temp_c"])) for r in rows]
        wind = [num(r["wind_kmh"]) for r in rows]
        self.wind = [None if w is None else w / 1.609344 for w in wind]
        self.water_mm = [num(r["rain_mm"]) for r in rows]     # METRo `ra`: liquid water on road
        self.qp_sn = [num(r["snow_rate"]) for r in rows]      # METRo `qp-sn`: 1 h snowfall, cm
        self.qp_ra = [num(r["rain_rate"]) for r in rows]      # METRo `qp-ra`: 1 h rain, mm
        self.rc = [int(r["road_condition_code"]) if (r.get("road_condition_code") or "").strip().isdigit()
                   else None for r in rows]
        if len(self.t) < 2:
            raise ValueError("roadcast has fewer than 2 steps")
        gaps = sorted((b - a).total_seconds() for a, b in zip(self.t, self.t[1:]))
        self.dt_h = gaps[len(gaps) // 2] / 3600.0   # typical step length in hours

    def __len__(self):
        return len(self.t)

    def wet(self, th) -> list[bool]:
        return [w is not None and w > th["wet_water_mm"] for w in self.water_mm]


class Forcing:
    """HRRR hourly values looked up at the nearest forecast hour."""

    def __init__(self, rows: list[dict]):
        rows = sorted(rows, key=lambda r: r["valid_time_utc"])
        self.t = [parse_time(r["valid_time_utc"]) for r in rows]
        self.tcc = [num(r["tcc_pct"]) for r in rows]
        self.cfrzr = [num(r["cfrzr"]) for r in rows]

    def _near(self, t: datetime):
        i = bisect.bisect_left(self.t, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self.t)]
        j = min(cands, key=lambda j: abs((self.t[j] - t).total_seconds()))
        # Don't extrapolate: the nearest hour must be within 90 minutes.
        return j if abs((self.t[j] - t).total_seconds()) <= 5400 else None

    def tcc_at(self, t):
        j = self._near(t)
        return None if j is None else self.tcc[j]

    def cfrzr_at(self, t):
        j = self._near(t)
        return None if j is None else self.cfrzr[j]


# ------------------------------------------------------------------ helpers
def runs(active: list[bool]) -> list[tuple[int, int]]:
    """Index ranges (first, last) of consecutive True values."""
    out, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(active) - 1))
    return out


def interval(flag: str, rc: Roadcast, i0: int, i1: int, score=None, detail: str = "") -> dict:
    return {
        "flag": flag,
        "name": FLAG_NAMES[flag],
        "start": iso(rc.t[i0]),
        "end": iso(rc.t[i1]),
        "hours": round((i1 - i0 + 1) * rc.dt_h, 2),
        "score": None if score is None else round(score, 2),
        "detail": detail,
    }


def interp(times: list[datetime], values: list, t: datetime):
    """Linear interpolation; None outside the range or across missing values."""
    i = bisect.bisect_left(times, t)
    if i < len(times) and times[i] == t:
        return values[i]
    if i == 0 or i == len(times):
        return None
    a, b = values[i - 1], values[i]
    if a is None or b is None:
        return None
    span = (times[i] - times[i - 1]).total_seconds()
    w = (t - times[i - 1]).total_seconds() / span
    return a + (b - a) * w


# -------------------------------------------------------------------- rules
def pavement_frost(rc: Roadcast, fc: Forcing | None, th) -> list[dict]:
    n = len(rc)
    cond = []
    for i in range(n):
        rst, dp = rc.rst[i], rc.dp[i]
        if rst is None or dp is None:
            cond.append(False)
            continue
        margin = 0.0
        if fc is not None and rc.wind[i] is not None:
            tcc = fc.tcc_at(rc.t[i])
            if tcc is not None and tcc < th["clear_cloud_pct"] and rc.wind[i] < th["calm_wind_mph"]:
                margin = th["frost_clear_calm_margin_f"]   # clear, calm: raise the bar
        cond.append(rst <= th["freezing_f"] and dp - rst > margin)
    out = []
    for i0, i1 in runs(cond):
        if (i1 - i0 + 1) * rc.dt_h + 1e-9 < th["frost_min_hours"]:
            continue
        score = sum((rc.dp[i] - rc.rst[i]) * rc.dt_h for i in range(i0, i1 + 1))
        out.append(interval("pavement_frost", rc, i0, i1, score,
                            "score = (dewpoint - RST) summed over time, F*h"))
    return out


def black_ice(rc: Roadcast, th) -> list[dict]:
    wet = rc.wet(th)
    frz = th["freezing_f"]
    out, k = [], 0
    for i in range(1, len(rc)):
        a, b = rc.rst[i - 1], rc.rst[i]
        if a is None or b is None or not (a > frz and b <= frz):
            continue
        if not (wet[i - 1] or wet[i]):
            continue
        k += 1
        j = i
        while j + 1 < len(rc) and rc.rst[j + 1] is not None and rc.rst[j + 1] <= frz:
            j += 1
        out.append(interval("black_ice", rc, i, j, k, f"wet road froze (crossing #{k} of 32F)"))
    return out


def flash_freeze(rc: Roadcast, th) -> list[dict]:
    w = max(1, round(th["flash_window_h"] / rc.dt_h))
    wet = rc.wet(th)
    wetish = [wet[i] or (rc.qp_ra[i] is not None and rc.qp_ra[i] > 0) for i in range(len(rc))]
    cond, drops = [False] * len(rc), [0.0] * len(rc)
    for i in range(w, len(rc)):
        a, b = rc.rst[i - w], rc.rst[i]
        if a is None or b is None:
            continue
        if a > th["freezing_f"] and b <= th["freezing_f"] and a - b >= th["flash_drop_f"] and any(wetish[i - w:i + 1]):
            cond[i], drops[i] = True, a - b
    return [interval("flash_freeze", rc, i0, i1, max(drops[i0:i1 + 1]),
                     "max RST drop in the 3 h window, F")
            for i0, i1 in runs(cond)]


def wheel_track_glazing(rc: Roadcast, th) -> list[dict]:
    fresh_steps = max(1, round(th["fresh_snow_hours"] / rc.dt_h))
    snowing = [s is not None and s > 0 for s in rc.qp_sn]
    cond = []
    for i in range(len(rc)):
        rst = rc.rst[i]
        if rst is None or rst > th["glaze_rst_f"]:
            cond.append(False)
            continue
        qp = rc.qp_sn[i]
        # cm of snow per hour -> inches of water per hour
        liquid_in_h = None if qp is None else (qp / th["snow_to_water_ratio"]) / 2.54
        light = liquid_in_h is not None and 0 < liquid_in_h < th["glaze_light_snow_in_per_h"]
        fresh = any(snowing[max(0, i - fresh_steps + 1):i + 1])
        blowing = rc.wind[i] is not None and rc.wind[i] >= th["glaze_blowing_wind_mph"] and fresh
        cond.append(light or blowing)
    return [interval("wheel_track_glazing", rc, i0, i1, None, "heavy-traffic site")
            for i0, i1 in runs(cond)]


def bridges_first(bridge: Roadcast, road: Roadcast, road_id: str, th) -> list[dict]:
    cond, gaps = [], []
    for i, t in enumerate(bridge.t):
        b = bridge.rst[i]
        r = interp(road.t, road.rst, t)
        ok = b is not None and r is not None and b <= th["freezing_f"] < r
        cond.append(ok)
        gaps.append(r - b if ok else 0.0)
    return [interval("bridges_first", bridge, i0, i1, max(gaps[i0:i1 + 1]),
                     f"bridge at or below freezing while {road_id} is above; score = largest gap, F")
            for i0, i1 in runs(cond)]


def salt_struggles(rc: Roadcast, th) -> list[dict]:
    codes = set(th["snow_ice_codes"])
    cond = [rc.rst[i] is not None and rc.rst[i] <= th["salt_rst_f"] and rc.rc[i] in codes
            for i in range(len(rc))]
    return [interval("salt_struggles", rc, i0, i1, None, "snow or ice on road with very cold pavement")
            for i0, i1 in runs(cond)]


def freezing_rain(rc: Roadcast, fc: Forcing, th) -> list[dict]:
    cond = []
    for i, t in enumerate(rc.t):
        p = fc.cfrzr_at(t)
        cond.append(p is not None and p >= th["freezing_rain_prob"]
                    and rc.rst[i] is not None and rc.rst[i] <= th["freezing_f"])
    return [interval("freezing_rain", rc, i0, i1, None, "HRRR freezing rain with RST at or below 32F")
            for i0, i1 in runs(cond)]


# ----------------------------------------------------------------- per site
def evaluate_site(site: dict, rc: Roadcast, fc: Forcing | None, road_by_id: dict[str, Roadcast], th) -> dict:
    flags: list[dict] = []
    evaluated: list[str] = []
    not_evaluated: dict[str, str] = {}

    def run(name, fn):
        evaluated.append(name)
        flags.extend(fn())

    run("pavement_frost", lambda: pavement_frost(rc, fc, th))
    run("black_ice", lambda: black_ice(rc, th))
    run("flash_freeze", lambda: flash_freeze(rc, th))
    run("salt_struggles", lambda: salt_struggles(rc, th))

    if site.get("traffic") == "heavy":
        run("wheel_track_glazing", lambda: wheel_track_glazing(rc, th))
    else:
        not_evaluated["wheel_track_glazing"] = "site is not marked traffic: heavy in sites.yaml"

    if site["station_type"] != "bridge":
        not_evaluated["bridges_first"] = "not a bridge site"
    elif not site.get("paired_road_site"):
        not_evaluated["bridges_first"] = "no paired_road_site in sites.yaml"
    elif site["paired_road_site"] not in road_by_id:
        not_evaluated["bridges_first"] = f"no roadcast for paired road site {site['paired_road_site']}"
    else:
        pid = site["paired_road_site"]
        run("bridges_first", lambda: bridges_first(rc, road_by_id[pid], pid, th))

    if fc is None:
        not_evaluated["freezing_rain"] = "no HRRR forcing file for this site"
    else:
        run("freezing_rain", lambda: freezing_rain(rc, fc, th))

    flags.sort(key=lambda f: (f["start"], f["flag"]))
    return {
        "name": site.get("name", site["id"]),
        "station_type": site["station_type"],
        "roadcast_start": iso(rc.t[0]),
        "roadcast_end": iso(rc.t[-1]),
        "flags": flags,
        "evaluated": evaluated,
        "not_evaluated": not_evaluated,
    }


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def newest_cycle(roadcast_dir: Path) -> str | None:
    tags = sorted({m.group(1) for p in roadcast_dir.glob("*.csv")
                   if (m := re.match(r"(\d{10}z)_", p.name))})
    return tags[-1] if tags else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--roadcast-dir", type=Path, default=Path("data/roadcast"))
    ap.add_argument("--forcing-dir", type=Path, default=Path("data/forcing"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/flags"))
    ap.add_argument("--latest", type=Path, default=Path("data/latest.json"),
                    help="pointer file telling the app which cycle is newest")
    ap.add_argument("--cycle", help="cycle tag, e.g. 2026110512z (default: newest roadcast)")
    args = ap.parse_args()

    tag = args.cycle or newest_cycle(args.roadcast_dir)
    if not tag or not re.fullmatch(r"\d{10}z", tag):
        log("no roadcast files found; nothing to flag")
        return 1
    cycle = datetime.strptime(tag[:-1], "%Y%m%d%H").replace(tzinfo=timezone.utc)

    sites = yaml.safe_load(args.sites.read_text())["sites"]
    roadcasts: dict[str, Roadcast] = {}
    forcings: dict[str, Forcing] = {}
    skipped: dict[str, str] = {}
    for s in sites:
        sid = s["id"]
        rpath = args.roadcast_dir / f"{tag}_{sid}.csv"
        if not rpath.exists():
            skipped[sid] = "no roadcast for this cycle (METRo skipped or failed)"
            continue
        try:
            roadcasts[sid] = Roadcast(read_csv(rpath))
        except (ValueError, KeyError) as exc:
            skipped[sid] = f"unreadable roadcast: {exc}"
            continue
        fpath = args.forcing_dir / f"hrrr_{tag}_{sid}.csv"
        if fpath.exists():
            forcings[sid] = Forcing(read_csv(fpath))

    if not roadcasts:
        # Normal when METRo had nothing to run on (e.g. no observations yet): not an error.
        log(f"cycle {tag}: no usable roadcasts; nothing to flag")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::No hazard flags for cycle {tag}: there were no roadcasts to flag", file=sys.stderr)
        return 0

    result_sites = {}
    for s in sites:
        sid = s["id"]
        if sid not in roadcasts:
            continue
        result_sites[sid] = evaluate_site(s, roadcasts[sid], forcings.get(sid), roadcasts, THRESHOLDS)
        n = len(result_sites[sid]["flags"])
        log(f"{sid}: {n} flag interval(s)")

    now = datetime.now(timezone.utc)
    out = {
        "cycle": tag,
        "cycle_utc": iso(cycle),
        "generated_utc": iso(now.replace(microsecond=0)),
        "units": UNITS,
        "thresholds": THRESHOLDS,
        "flag_names": FLAG_NAMES,
        "sites": result_sites,
        "skipped_sites": skipped,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{tag}.json").write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {args.out_dir / (tag + '.json')}")

    # Update the "latest" pointer, but never move it backwards to an older cycle.
    prev = None
    if args.latest.exists():
        try:
            prev = json.loads(args.latest.read_text()).get("cycle")
        except (ValueError, OSError):
            prev = None
    if prev is None or tag >= prev:
        args.latest.parent.mkdir(parents=True, exist_ok=True)
        args.latest.write_text(json.dumps({
            "cycle": tag,
            "cycle_utc": iso(cycle),
            "generated_utc": iso(now.replace(microsecond=0)),
            "sites": list(result_sites),
        }, indent=2) + "\n")
        log(f"updated {args.latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
