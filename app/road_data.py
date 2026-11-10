"""Load the published forecast files from the `data` branch (no Streamlit here).

Everything is fetched over HTTPS from raw.githubusercontent.com, so the app
needs no GitHub login and never touches the repository itself.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone

import pandas as pd
import requests

REPO = "justingehrts/road-weather"
BRANCH = "data"
# Can be overridden (e.g. to test against a local web server).
DATA_BASE = os.environ.get(
    "ROAD_WEATHER_DATA_URL", f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
).rstrip("/") + "/"
DISPLAY_TZ = "America/New_York"   # data stays UTC; only shown in Eastern


class DataError(Exception):
    """The data branch could not be reached (as opposed to a file not existing yet)."""


def fetch_text(path: str, base: str = DATA_BASE, timeout: int = 20) -> str | None:
    """Return the file's text, or None if it does not exist (HTTP 404)."""
    try:
        resp = requests.get(base + path, timeout=timeout)
    except requests.RequestException as exc:
        raise DataError(f"Could not reach {base + path}: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise DataError(f"{base + path} returned HTTP {resp.status_code}")
    return resp.text


def load_json(path: str, base: str = DATA_BASE):
    text = fetch_text(path, base)
    return None if text is None else json.loads(text)


def load_roadcast(cycle: str, site: str, base: str = DATA_BASE) -> pd.DataFrame | None:
    """METRo roadcast for one site, temperatures in degrees F."""
    text = fetch_text(f"data/roadcast/{cycle}_{site}.csv", base)
    if text is None:
        return None
    df = pd.read_csv(io.StringIO(text))
    df["time_utc"] = pd.to_datetime(df["roadcast_time_utc"], utc=True)
    for c_col, f_col in (("road_temp_c", "road_temp_f"), ("air_temp_c", "air_temp_f"),
                         ("dewpoint_c", "dewpoint_f"), ("subsurface_temp_c", "subsurface_temp_f")):
        df[f_col] = df[c_col] * 9 / 5 + 32
    df["wind_mph"] = df["wind_kmh"] / 1.609344
    return df


OBS_COLS = ["road_temp_f", "air_temp_f", "dewpoint_f"]


def load_obs(site: str, since: pd.Timestamp, base: str = DATA_BASE) -> pd.DataFrame:
    """Observed road surface, air and dewpoint temperatures (degrees F) from `since` onward.

    A row is kept if at least one of the three has a value; missing ones stay NaN.
    """
    text = fetch_text(f"data/obs/{site}.csv", base)
    if text is None:
        return pd.DataFrame(columns=["time_utc", *OBS_COLS])
    df = pd.read_csv(io.StringIO(text))
    for col in OBS_COLS:
        if col not in df.columns:
            df[col] = float("nan")
    df["time_utc"] = pd.to_datetime(df["obs_time_utc"], utc=True)
    df = df[df["time_utc"] >= since].dropna(subset=OBS_COLS, how="all")
    return df[["time_utc", *OBS_COLS]].sort_values("time_utc").reset_index(drop=True)


def to_eastern(series: pd.Series) -> pd.Series:
    """UTC timestamps -> naive Eastern wall-clock times (what the charts plot)."""
    return pd.to_datetime(series, utc=True).dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)


def now_eastern() -> pd.Timestamp:
    return pd.Timestamp.now(tz=timezone.utc).tz_convert(DISPLAY_TZ).tz_localize(None)


def flag_intervals(site_flags: dict) -> pd.DataFrame:
    """One row per active flag interval, with Eastern start/end times."""
    cols = ["flag", "name", "start", "end", "hours", "score", "detail"]
    rows = site_flags.get("flags", [])
    if not rows:
        return pd.DataFrame(columns=cols + ["start_et", "end_et"])
    df = pd.DataFrame(rows)[cols]
    df["start_et"] = to_eastern(df["start"])
    df["end_et"] = to_eastern(df["end"])
    return df


def age_hours(generated_utc: str) -> float:
    t = datetime.strptime(generated_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
