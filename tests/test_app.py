"""Smoke tests for the Streamlit app, fed by real flags.py output (no network)."""
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import charts
import flags
import road_data as rd

APP = str(Path(__file__).resolve().parent.parent / "app" / "forecast.py")
START = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
CYCLE = "2026110512z"

ROADCAST_COLS = ["cycle_utc", "roadcast_time_utc", "air_temp_c", "dewpoint_c", "wind_kmh", "snow_cm", "rain_mm",
                 "snow_rate", "rain_rate", "cloud_octas", "solar_wm2", "infrared_wm2", "road_temp_c",
                 "subsurface_temp_c", "road_condition_code", "road_condition"]


def build_data(root):
    """Write a small cold-weather data tree the way the pipeline would."""
    for d in ("roadcast", "forcing", "obs", "flags"):
        (root / "data" / d).mkdir(parents=True)
    (root / "sites.yaml").write_text(
        "sites:\n  - {id: s1, name: Test Road, lat: 40, lon: -83, ohgo_site_id: x, station_type: road,"
        " pavement_layers: [{material: asphalt, thickness_m: 0.1}]}\n")
    with (root / "data/roadcast" / f"{CYCLE}_s1.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROADCAST_COLS)
        w.writeheader()
        for i in range(30):
            w.writerow({"roadcast_time_utc": flags.iso(START + timedelta(minutes=20 * i)),
                        "road_temp_c": -9, "dewpoint_c": -6, "air_temp_c": -8, "subsurface_temp_c": -3,
                        "wind_kmh": 5, "rain_mm": 0, "snow_rate": 0, "rain_rate": 0,
                        "road_condition_code": 3, "road_condition": "ice_snow"})
    with (root / "data/obs/s1.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["obs_time_utc", "air_temp_f", "dewpoint_f", "wind_avg_mph", "wind_max_mph", "wind_dir",
                    "road_temp_f", "subsurface_temp_f", "surface_condition", "surface_sensor_status"])
        for h in range(-6, 0):
            w.writerow([flags.iso(START + timedelta(hours=h)), 20, 15, 5, 8, "N", 18 + h, 30, "", "OK"])
    argv = ["flags.py", "--sites", str(root / "sites.yaml"), "--roadcast-dir", str(root / "data/roadcast"),
            "--forcing-dir", str(root / "data/forcing"), "--out-dir", str(root / "data/flags"),
            "--latest", str(root / "data/latest.json")]
    old, sys.argv = sys.argv, argv
    try:
        assert flags.main() == 0
    finally:
        sys.argv = old


def serve_from(monkeypatch, root):
    def fake_fetch(path, base=None, timeout=20):
        p = root / path
        return p.read_text() if p.exists() else None
    monkeypatch.setattr(rd, "fetch_text", fake_fetch)


@pytest.fixture(autouse=True)
def fresh_cache():
    """The app caches loaded data for 5 minutes; start every test with an empty cache."""
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run_app():
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    return at


def test_page_renders_a_published_forecast(tmp_path, monkeypatch):
    build_data(tmp_path)
    serve_from(monkeypatch, tmp_path)
    at = run_app()
    assert not at.exception
    assert at.selectbox[0].value == "s1"
    text = " ".join(m.value for m in at.markdown)
    assert "12Z HRRR" in text
    assert not at.warning                     # fresh forecast: no staleness warning


def test_page_warns_when_forecast_is_old(tmp_path, monkeypatch):
    build_data(tmp_path)
    p = tmp_path / "data/flags" / f"{CYCLE}.json"
    d = json.loads(p.read_text())
    d["generated_utc"] = flags.iso(datetime.now(timezone.utc) - timedelta(days=3))
    p.write_text(json.dumps(d))
    serve_from(monkeypatch, tmp_path)
    at = run_app()
    assert not at.exception
    assert any("older" in w.value or "more than 9 hours" in w.value for w in at.warning)


def test_page_handles_no_published_data(monkeypatch, tmp_path):
    serve_from(monkeypatch, tmp_path)          # empty data branch
    at = run_app()
    assert not at.exception
    assert any("No forecast has been published" in i.value for i in at.info)


def test_page_reports_unreachable_data(monkeypatch):
    def boom(*a, **k):
        raise rd.DataError("no network")
    monkeypatch.setattr(rd, "fetch_text", boom)
    at = run_app()
    assert not at.exception
    assert any("Could not load" in e.value for e in at.error)


# ------------------------------------------------------------------ helpers
def test_label_spreading_keeps_order_and_min_gap():
    out = charts._spread([10.0, 10.5, 11.0, 30.0], 2.0)
    assert out == sorted(out)
    assert all(b - a >= 2.0 - 1e-9 for a, b in zip(out, out[1:]))
    assert out[-1] == 30.0                      # far-away labels are not moved


def test_flag_intervals_converts_to_eastern(tmp_path):
    iv = rd.flag_intervals({"flags": [{"flag": "x", "name": "X", "start": "2026-11-05T15:00:00Z",
                                       "end": "2026-11-05T16:00:00Z", "hours": 1.33, "score": None, "detail": ""}]})
    assert iv["start_et"].iloc[0] == pd.Timestamp("2026-11-05 10:00:00")    # EST = UTC-5
    assert rd.flag_intervals({"flags": []}).empty
