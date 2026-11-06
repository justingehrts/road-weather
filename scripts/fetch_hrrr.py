#!/usr/bin/env python3
"""Download HRRR forcing for each site in sites.yaml (nearest grid point).

Rules this script enforces:
  * Only the synoptic cycles 00, 06, 12 and 18Z are used (they are the only
    HRRR runs that go out to 48 hours). Never an off-hour cycle.
  * The target is the most recent such cycle, and only once forecast hour 48
    is available on AWS (checked via the GRIB .idx inventory). If it is not
    there yet we wait 10 minutes and retry, giving up after 60 minutes.
  * No partial runs: all of f00..f48 must download and parse for every field
    before any CSV is written.

Only the fields we need are downloaded (Herbie byte-range "search"), not the
whole 130 MB file. Output units are the raw HRRR ones (K, m/s, Pa, W/m2, %).
Output: <out-dir>/hrrr_<YYYYMMDDHH>z_<site id>.csv
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

warnings.filterwarnings("ignore")  # cfgrib/xarray FutureWarnings are very noisy

SYNOPTIC_HOURS = (0, 6, 12, 18)
LAST_FXX = 48
WAIT_MINUTES = 10
GIVE_UP_MINUTES = 60
DOWNLOAD_ATTEMPTS = 5          # per forecast hour; AWS S3 sometimes answers 503 for a moment
RETRY_WAIT_SECONDS = 5         # doubles each time: 5, 10, 20, 40

# Herbie "search" regex, matched against the .idx inventory lines.
# 2 m T and Td, 10 m u/v wind, precip rate, categorical precip type
# (rain / freezing rain / ice pellets / snow), total cloud cover,
# downward shortwave + longwave radiation, surface pressure.
SEARCH = (
    r":(?:TMP|DPT):2 m above ground"
    r"|:(?:UGRD|VGRD):10 m above ground"
    r"|:PRATE:surface"
    r"|:(?:CRAIN|CFRZR|CICEP|CSNOW):surface"
    r"|:TCDC:entire atmosphere"
    r"|:DSWRF:surface"
    r"|:DLWRF:surface"
    r"|:PRES:surface"
)
N_FIELDS = 13  # messages the search above must match in every file

# cfgrib variable name -> output column
VARS = {
    "t2m": "t2m_k",
    "d2m": "d2m_k",
    "u10": "u10_ms",
    "v10": "v10_ms",
    "prate": "prate_kgm2s",
    "crain": "crain",
    "cfrzr": "cfrzr",
    "cicep": "cicep",
    "csnow": "csnow",
    "tcc": "tcc_pct",
    "sdswrf": "dswrf_wm2",
    "sdlwrf": "dlwrf_wm2",
    "sp": "sp_pa",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def latest_cycle(now: datetime) -> datetime:
    """Most recent 00/06/12/18Z cycle at or before `now` (UTC)."""
    hour = max(h for h in SYNOPTIC_HOURS if h <= now.hour)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def make_herbie(cycle: datetime, fxx: int, cache_dir: Path):
    from herbie import Herbie   # imported here so the module loads (and its logic can be tested) without Herbie
    return Herbie(
        cycle.strftime("%Y-%m-%d %H:%M"),
        model="hrrr",
        product="sfc",
        fxx=fxx,
        priority=["aws"],
        save_dir=cache_dir,
        verbose=False,
    )


def hour_available(cycle: datetime, fxx: int, cache_dir: Path) -> bool:
    """True if the AWS file + index exist and contain all our fields."""
    try:
        H = make_herbie(cycle, fxx, cache_dir)
        if H.grib is None or H.idx is None:
            return False
        return len(H.inventory(SEARCH)) == N_FIELDS
    except Exception as exc:  # missing .idx, network blip, etc.
        log(f"  f{fxx:02d} not ready ({type(exc).__name__}: {exc})")
        return False


def wait_for_f48(cycle: datetime, cache_dir: Path) -> bool:
    deadline = time.monotonic() + GIVE_UP_MINUTES * 60
    while True:
        if hour_available(cycle, LAST_FXX, cache_dir):
            log(f"f{LAST_FXX} is available for {cycle:%Y-%m-%d %HZ}")
            return True
        if time.monotonic() + WAIT_MINUTES * 60 > deadline:
            return False
        log(f"f{LAST_FXX} for {cycle:%Y-%m-%d %HZ} not there yet; "
            f"waiting {WAIT_MINUTES} min")
        time.sleep(WAIT_MINUTES * 60)


def nearest_index(lat2d, lon2d, lat: float, lon: float) -> tuple[int, int]:
    """Nearest grid cell by great-circle (haversine) distance."""
    lon360 = lon % 360
    p1, p2 = np.radians(lat2d), np.radians(lat)
    dlon = np.radians(lon2d - lon360)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return np.unravel_index(np.argmin(a), a.shape)


def ptype(row) -> str:
    # Most hazardous wins when HRRR flags more than one.
    for col, name in (("cfrzr", "freezing_rain"), ("cicep", "ice_pellets"),
                      ("csnow", "snow"), ("crain", "rain")):
        if row[col] >= 0.5:
            return name
    return "none"


def read_hour(cycle: datetime, fxx: int, cache_dir: Path) -> list:
    """Download and open one forecast hour, retrying brief network/server errors.

    A failed attempt can leave a half-written file behind, so every retry
    re-downloads (overwrite=True) instead of trusting what is on disk. If all
    attempts fail the error is raised, which stops the whole run: we never
    publish a partial forecast.
    """
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            H = make_herbie(cycle, fxx, cache_dir)
            ds = H.xarray(SEARCH, overwrite=attempt > 1)
            return ds if isinstance(ds, list) else [ds]
        except Exception as exc:
            if attempt == DOWNLOAD_ATTEMPTS:
                log(f"f{fxx:02d}: giving up after {DOWNLOAD_ATTEMPTS} attempts")
                raise
            wait = RETRY_WAIT_SECONDS * 2 ** (attempt - 1)
            log(f"f{fxx:02d}: attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed "
                f"({type(exc).__name__}: {exc}); retrying in {wait}s")
            time.sleep(wait)


def load_sites(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())["sites"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/forcing"))
    ap.add_argument("--cache-dir", type=Path, default=Path(".herbie_cache"))
    ap.add_argument("--cycle", help="Override cycle, e.g. 2026-11-05T12 (must be 00/06/12/18)")
    args = ap.parse_args()

    if args.cycle:
        cycle = datetime.fromisoformat(args.cycle).replace(tzinfo=None)
        if cycle.hour not in SYNOPTIC_HOURS or cycle.minute:
            log("--cycle must be exactly 00, 06, 12 or 18Z")
            return 1
    else:
        cycle = latest_cycle(datetime.now(timezone.utc)).replace(tzinfo=None)
    tag = cycle.strftime("%Y%m%d%H") + "z"

    sites = load_sites(args.sites)
    outputs = {s["id"]: args.out_dir / f"hrrr_{tag}_{s['id']}.csv" for s in sites}
    if all(p.exists() for p in outputs.values()):
        log(f"cycle {tag}: all forcing files already exist; nothing to do")
        return 0

    log(f"target cycle: {cycle:%Y-%m-%d %H}Z")
    if not wait_for_f48(cycle, args.cache_dir):
        log(f"gave up: f{LAST_FXX} for {tag} still unavailable after "
            f"{GIVE_UP_MINUTES} minutes. No files written.")
        return 1

    rows: dict[str, list[dict]] = {s["id"]: [] for s in sites}
    idx: dict[str, tuple[int, int]] = {}
    for fxx in range(LAST_FXX + 1):
        datasets = read_hour(cycle, fxx, args.cache_dir)
        found: dict[str, object] = {}
        for ds in datasets:
            for v in ds.data_vars:
                if v in VARS:
                    found[v] = ds[v]
        missing = set(VARS) - set(found)
        if missing:
            log(f"f{fxx:02d}: missing fields {sorted(missing)}; aborting, no files written")
            return 1

        ref = found["t2m"]
        if not idx:
            lat2d, lon2d = ref["latitude"].values, ref["longitude"].values
            for s in sites:
                idx[s["id"]] = nearest_index(lat2d, lon2d, s["lat"], s["lon"])
        valid = pd.Timestamp(ref["valid_time"].values).tz_localize("UTC")

        for s in sites:
            j, i = idx[s["id"]]
            row = {
                "cycle_utc": cycle.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fxx": fxx,
                "valid_time_utc": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for var, col in VARS.items():
                row[col] = float(found[var].values[j, i])
            row["grid_lat"] = round(float(ref["latitude"].values[j, i]), 4)
            row["grid_lon"] = round(float(ref["longitude"].values[j, i]) - 360, 4)
            row["ptype"] = ptype(row)
            rows[s["id"]].append(row)
        log(f"f{fxx:02d} done")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for sid, data in rows.items():
        assert len(data) == LAST_FXX + 1, f"{sid}: expected {LAST_FXX + 1} rows"
        pd.DataFrame(data).to_csv(outputs[sid], index=False, float_format="%.6g")
        log(f"wrote {outputs[sid]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
