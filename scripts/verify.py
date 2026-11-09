#!/usr/bin/env python3
"""Verify past METRo roadcasts against the RWIS observations that came later.

For every past roadcast (data/roadcast/<cycle>_<site>.csv) this
  1. interpolates the forecast to each later observation time and records the
     error (forecast minus observed) for road surface temperature and air
     temperature, grouped by site and lead time (hours since the HRRR cycle);
  2. checks each hazard flag (data/flags/<cycle>.json) against what the RWIS
     station reported at those times and counts hits, misses and false alarms.

Writes data/verification/summary.json, which the Streamlit Verification page
reads. It recomputes the last --days days from scratch each run, so it is safe
to run repeatedly. Standard library + PyYAML only (Python 3.11).

Method notes are in docs/verification.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from flags import FLAG_NAMES, c_to_f, interp, iso, num, parse_time

LEAD_EDGES = [0, 6, 12, 18, 24, 30, 36, 42, 48]   # hours after the HRRR cycle time
LEAD_LABELS = [f"{a}-{b}" for a, b in zip(LEAD_EDGES, LEAD_EDGES[1:])]
FREEZING_F = 32.0
FLASH_LOOKBACK_H = 3.0          # a "wet -> ice" change must happen within this many hours
MAX_INTERP_GAP_H = 2.0          # don't interpolate observations across a gap longer than this
LOW_SAMPLE_EVENTS = 10          # fewer observed events than this = treat rates as shaky

TEMP_VARS = {
    "road_temp_f": "Road surface temperature",
    "air_temp_f": "Air temperature",
}

# What each flag is verified against (see the "Verify against" column of docs/flags.md).
FLAG_VERIFICATION = {
    "pavement_frost": "RWIS surface condition reports frost",
    "black_ice": "RWIS surface condition reports ice / black ice",
    "flash_freeze": "RWIS condition changes from wet to ice within 3 h",
    "wheel_track_glazing": "RWIS surface condition reports ice or snow (crash/traffic-speed data not available)",
    "bridges_first": "Observed bridge road temp at or below 32F while its paired road site is above 32F",
    "freezing_rain": "RWIS surface condition reports ice (precipitation type is not observed)",
}
NOT_VERIFIED = {
    "salt_struggles": "Qualitative rule: there is no RWIS measurement to check it against.",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Placeholder values some stations send when they have nothing to report.
NO_INFORMATION = {"unknown", "error", "other", "n/a", "na", "none", "not reported", "no report"}


# ------------------------------------------------- RWIS condition vocabulary
def classify_condition(text: str | None) -> set[str] | None:
    """Map an RWIS surface-condition string to {frost, ice, snow, wet, dry}.

    Returns None when the station reported nothing (blank), so those
    observations are skipped rather than counted as "no hazard". "Watch"
    states (e.g. "Ice Watch") mean ice is possible, not present, so they do
    not count as ice or snow. Adjust the words here if OHGO uses others.
    """
    t = (text or "").strip().lower()
    if not t or t in NO_INFORMATION:
        return None
    cats: set[str] = set()
    watch = "watch" in t
    if "frost" in t:
        cats.add("frost")
    if re.search(r"\bice\b|\bicy\b|black ice", t) and not watch:
        cats.add("ice")
    if ("snow" in t or "slush" in t) and not watch:
        cats.add("snow")
    if re.search(r"\bwet\b|damp|moist|slush", t):
        cats.add("wet")
    if re.search(r"\bdry\b", t):
        cats.add("dry")
    return cats


# ------------------------------------------------------------------ loading
class Obs:
    """One site's RWIS observations, sorted by time."""

    def __init__(self, rows: list[dict]):
        parsed = []
        for r in rows:
            try:
                t = parse_time(r["obs_time_utc"])
            except (KeyError, ValueError):
                continue
            parsed.append((t, num(r.get("road_temp_f")), num(r.get("air_temp_f")),
                           (r.get("surface_condition") or "").strip()))
        parsed.sort(key=lambda x: x[0])
        self.t = [p[0] for p in parsed]
        self.rst = [p[1] for p in parsed]
        self.air = [p[2] for p in parsed]
        self.cond = [p[3] for p in parsed]
        self.cats = [classify_condition(c) for c in self.cond]

    def __len__(self):
        return len(self.t)


class Roadcast:
    def __init__(self, rows: list[dict]):
        rows = sorted(rows, key=lambda r: r["roadcast_time_utc"])
        self.t = [parse_time(r["roadcast_time_utc"]) for r in rows]
        self.road_temp_f = [c_to_f(num(r["road_temp_c"])) for r in rows]
        self.air_temp_f = [c_to_f(num(r["air_temp_c"])) for r in rows]
        gaps = sorted((b - a).total_seconds() for a, b in zip(self.t, self.t[1:])) or [1200.0]
        self.step = timedelta(seconds=gaps[len(gaps) // 2])


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------- accumulators
class ErrAcc:
    """Running sums for bias / RMSE / MAE."""

    def __init__(self):
        self.n, self.s, self.ss, self.sa = 0, 0.0, 0.0, 0.0

    def add(self, e: float):
        self.n += 1
        self.s += e
        self.ss += e * e
        self.sa += abs(e)

    def merge(self, o: "ErrAcc"):
        self.n += o.n
        self.s += o.s
        self.ss += o.ss
        self.sa += o.sa

    def stats(self) -> dict:
        if self.n == 0:
            return {"n": 0, "bias": None, "rmse": None, "mae": None}
        return {"n": self.n, "bias": round(self.s / self.n, 2),
                "rmse": round(math.sqrt(self.ss / self.n), 2), "mae": round(self.sa / self.n, 2)}


def lead_label(hours: float) -> str | None:
    for lo, hi, label in zip(LEAD_EDGES, LEAD_EDGES[1:], LEAD_LABELS):
        if lo <= hours < hi or (hi == LEAD_EDGES[-1] and hours == hi):
            return label
    return None


def new_counts() -> dict:
    return {"hits": 0, "misses": 0, "false_alarms": 0, "correct_negatives": 0}


def rates(c: dict) -> dict:
    h, m, fa, cn = c["hits"], c["misses"], c["false_alarms"], c["correct_negatives"]

    def ratio(a, b):
        return None if b == 0 else round(a / b, 3)

    return {**c,
            "observed_events": h + m,
            "forecast_events": h + fa,
            "total": h + m + fa + cn,
            "hit_rate": ratio(h, h + m),                 # of the events that happened, how many were flagged
            "false_alarm_ratio": ratio(fa, h + fa),      # of the flagged times, how many had no event
            "false_alarm_rate": ratio(fa, fa + cn),      # of the no-event times, how many were flagged
            "low_sample": (h + m) < LOW_SAMPLE_EVENTS}


# ------------------------------------------------------------------ the work
def flag_active(intervals: list[tuple[datetime, datetime]], t: datetime, step: timedelta) -> bool:
    return any(s <= t < e + step for s, e in intervals)


def observed_events(obs: Obs, i: int) -> dict[str, bool | None]:
    """Condition-based observed events at observation i (None = cannot tell)."""
    cats = obs.cats[i]
    if cats is None:
        return {"pavement_frost": None, "black_ice": None, "flash_freeze": None,
                "wheel_track_glazing": None, "freezing_rain": None}
    ice = "ice" in cats
    wet_before = False
    if ice:
        lo = obs.t[i] - timedelta(hours=FLASH_LOOKBACK_H)
        wet_before = any(obs.cats[j] is not None and "wet" in obs.cats[j]
                         for j in range(len(obs)) if lo <= obs.t[j] <= obs.t[i])
    return {
        "pavement_frost": "frost" in cats,
        "black_ice": ice,
        "flash_freeze": ice and wet_before,
        "wheel_track_glazing": ice or "snow" in cats,
        "freezing_rain": ice,
    }


def interp_obs(times: list[datetime], values: list, t: datetime):
    """Interpolate observations, but never across a long gap."""
    import bisect
    i = bisect.bisect_left(times, t)
    if i < len(times) and times[i] == t:
        return values[i]
    if i == 0 or i == len(times):
        return None
    if (times[i] - times[i - 1]).total_seconds() > MAX_INTERP_GAP_H * 3600:
        return None
    return interp(times, values, t)


def verify(sites: list[dict], roadcast_dir: Path, flags_dir: Path, obs_dir: Path,
           now: datetime, days: int) -> dict:
    cutoff = now - timedelta(days=days)
    by_id = {s["id"]: s for s in sites}
    obs_by_site: dict[str, Obs] = {}
    for sid in by_id:
        p = obs_dir / f"{sid}.csv"
        if p.exists():
            obs_by_site[sid] = Obs(read_csv(p))

    # accumulators
    err: dict[tuple[str, str, str], ErrAcc] = {}          # (var, site, lead)
    cont: dict[tuple[str, str], dict] = {}                # (flag, site)
    cycles_used: set[str] = set()
    event_cache: dict[tuple[str, int], dict] = {}
    pairs = 0

    files = sorted(roadcast_dir.glob("*.csv"))
    for path in files:
        m = re.match(r"(\d{10}z)_(.+)\.csv$", path.name)
        if not m:
            continue
        tag, sid = m.group(1), m.group(2)
        cycle = datetime.strptime(tag[:-1], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if cycle < cutoff or sid not in by_id or sid not in obs_by_site:
            continue
        try:
            rc = Roadcast(read_csv(path))
        except (KeyError, ValueError):
            continue
        if len(rc.t) < 2:
            continue
        obs = obs_by_site[sid]

        # forecast flags for this cycle/site
        fl_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
        evaluated: list[str] = []
        fpath = flags_dir / f"{tag}.json"
        if fpath.exists():
            try:
                fd = json.loads(fpath.read_text())["sites"].get(sid)
            except (ValueError, KeyError):
                fd = None
            if fd:
                evaluated = list(fd.get("evaluated", []))
                for f in fd.get("flags", []):
                    fl_intervals.setdefault(f["flag"], []).append((parse_time(f["start"]), parse_time(f["end"])))

        used_any = False
        for i, t_o in enumerate(obs.t):
            if t_o < rc.t[0] or t_o > rc.t[-1]:
                continue          # only observations that fall inside the roadcast period (and so came after it began)
            lead = lead_label((t_o - cycle).total_seconds() / 3600.0)
            if lead is None:
                continue

            # --- temperature errors
            for var, obs_vals, fc_vals in (("road_temp_f", obs.rst, rc.road_temp_f),
                                           ("air_temp_f", obs.air, rc.air_temp_f)):
                o = obs_vals[i]
                f = interp(rc.t, fc_vals, t_o)
                if o is None or f is None:
                    continue
                err.setdefault((var, sid, lead), ErrAcc()).add(f - o)
                used_any = True
                if var == "road_temp_f":
                    pairs += 1          # "pairs" counts matched road-temperature observations

            # --- flags
            if (sid, i) not in event_cache:      # same observation is reused by many roadcasts
                event_cache[(sid, i)] = observed_events(obs, i)
            events = event_cache[(sid, i)]
            for flag in evaluated:
                if flag == "bridges_first":
                    pair_id = by_id[sid].get("paired_road_site")
                    road = obs_by_site.get(pair_id) if pair_id else None
                    b = obs.rst[i]
                    r = interp_obs(road.t, road.rst, t_o) if road is not None else None
                    happened = None if (b is None or r is None) else (b <= FREEZING_F < r)
                else:
                    happened = events.get(flag)
                if happened is None:
                    continue
                yes = flag_active(fl_intervals.get(flag, []), t_o, rc.step)
                c = cont.setdefault((flag, sid), new_counts())
                if yes and happened:
                    c["hits"] += 1
                elif yes:
                    c["false_alarms"] += 1
                elif happened:
                    c["misses"] += 1
                else:
                    c["correct_negatives"] += 1
        if used_any:
            cycles_used.add(tag)

    # ---------------------------------------------------------- assemble output
    out: dict = {
        "generated_utc": iso(now.replace(microsecond=0)),
        "window_days": days,
        "lead_bins": LEAD_LABELS,
        "cycles_verified": len(cycles_used),
        "matched_pairs": pairs,
        "temperature": {},
        "flags": {},
    }

    for var, label in TEMP_VARS.items():
        site_ids = sorted({k[1] for k in err if k[0] == var})
        overall_by_lead = {lb: ErrAcc() for lb in LEAD_LABELS}
        sites_out = {}
        for sid in site_ids:
            site_all = ErrAcc()
            by_lead = []
            for lb in LEAD_LABELS:
                a = err.get((var, sid, lb))
                if a is None:
                    continue
                by_lead.append({"lead": lb, **a.stats()})
                site_all.merge(a)
                overall_by_lead[lb].merge(a)
            sites_out[sid] = {"name": by_id[sid].get("name", sid), "station_type": by_id[sid]["station_type"],
                              "overall": site_all.stats(), "by_lead": by_lead}
        out["temperature"][var] = {
            "label": label,
            "overall_by_lead": [{"lead": lb, **a.stats()} for lb, a in overall_by_lead.items() if a.n],
            "by_site": sites_out,
        }

    obs_total = obs_with_cond = 0
    for sid, obs in obs_by_site.items():
        for t, c in zip(obs.t, obs.cats):
            if t >= cutoff:
                obs_total += 1
                obs_with_cond += c is not None
    out["flags"]["condition_data"] = {"observations": obs_total, "with_surface_condition": obs_with_cond}

    by_flag = {}
    for flag, verified_against in FLAG_VERIFICATION.items():
        site_counts = {sid: c for (f, sid), c in cont.items() if f == flag}
        if not site_counts:
            by_flag[flag] = {"name": FLAG_NAMES[flag], "verified_against": verified_against,
                             "overall": None, "by_site": {}}
            continue
        total = new_counts()
        for c in site_counts.values():
            for k in total:
                total[k] += c[k]
        by_flag[flag] = {
            "name": FLAG_NAMES[flag], "verified_against": verified_against,
            "overall": rates(total),
            "by_site": {sid: {"name": by_id[sid].get("name", sid), **rates(c)} for sid, c in sorted(site_counts.items())},
        }
    out["flags"]["by_flag"] = by_flag
    out["flags"]["not_verified"] = {FLAG_NAMES[k]: v for k, v in NOT_VERIFIED.items()}
    out["flags"]["definitions"] = {
        "hit_rate": "Of the times the hazard was observed, the share that had the flag on (higher is better).",
        "false_alarm_ratio": "Of the times the flag was on, the share where the hazard was not observed (lower is better).",
        "false_alarm_rate": "Of the times the hazard was not observed, the share where the flag was on (lower is better).",
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--roadcast-dir", type=Path, default=Path("data/roadcast"))
    ap.add_argument("--flags-dir", type=Path, default=Path("data/flags"))
    ap.add_argument("--obs-dir", type=Path, default=Path("data/obs"))
    ap.add_argument("--out", type=Path, default=Path("data/verification/summary.json"))
    ap.add_argument("--days", type=int, default=30, help="verify roadcasts from the last N days")
    ap.add_argument("--now", help="override 'now' (ISO, UTC) for testing")
    args = ap.parse_args()

    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    sites = yaml.safe_load(args.sites.read_text())["sites"]
    result = verify(sites, args.roadcast_dir, args.flags_dir, args.obs_dir, now, args.days)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    cd = result["flags"]["condition_data"]
    log(f"verified {result['cycles_verified']} cycle(s), {result['matched_pairs']} forecast/observation pairs")
    log(f"observations with a surface condition: {cd['with_surface_condition']} of {cd['observations']}")
    if result["matched_pairs"] == 0:
        log("nothing to verify yet (need roadcasts plus later observations)")
    log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
