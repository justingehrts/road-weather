"""Tests for scripts/flags.py: one scenario per hazard rule, plus end-to-end."""
import json
import sys
from datetime import datetime, timedelta, timezone

import flags
from flags import THRESHOLDS as TH

START = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def make_rc(rst_f, dp_f=None, wind_mph=0.0, water_mm=0.0, qp_sn=0.0, qp_ra=0.0, rc=1, start=START):
    """Build a Roadcast from per-step lists (degrees F / mph). Scalars are repeated."""
    n = len(rst_f)

    def seq(v):
        return list(v) if isinstance(v, (list, tuple)) else [v] * n

    dp, wind, water, sn, ra, code = map(seq, (dp_f if dp_f is not None else [20.0] * n,
                                              wind_mph, water_mm, qp_sn, qp_ra, rc))
    rows = []
    for i in range(n):
        rows.append({
            "roadcast_time_utc": flags.iso(start + timedelta(minutes=20 * i)),
            "road_temp_c": f_to_c(rst_f[i]),
            "dewpoint_c": f_to_c(dp[i]),
            "air_temp_c": f_to_c(rst_f[i]),
            "wind_kmh": wind[i] * 1.609344,
            "rain_mm": water[i],
            "snow_rate": sn[i],
            "rain_rate": ra[i],
            "road_condition_code": str(code[i]),
        })
    return flags.Roadcast(rows)


def make_fc(tcc=100.0, cfrzr=0.0, hours=12, start=START):
    rows = [{"valid_time_utc": flags.iso(start + timedelta(hours=h)), "tcc_pct": tcc, "cfrzr": cfrzr}
            for h in range(hours)]
    return flags.Forcing(rows)


# ------------------------------------------------------------ pavement frost
def test_frost_needs_two_hours():
    long_run = make_rc([30] * 8, dp_f=[33] * 8)       # 8 steps = 2 h 40
    short_run = make_rc([30] * 5, dp_f=[33] * 5)      # 5 steps = 1 h 40
    out = flags.pavement_frost(long_run, None, TH)
    assert len(out) == 1 and out[0]["hours"] == round(8 / 3, 2)
    assert out[0]["score"] == round(3 * 8 / 3, 2)      # (33-30) F summed over 2.67 h
    assert flags.pavement_frost(short_run, None, TH) == []


def test_frost_needs_dewpoint_above_rst_and_freezing():
    assert flags.pavement_frost(make_rc([30] * 8, dp_f=[28] * 8), None, TH) == []   # dry air
    assert flags.pavement_frost(make_rc([34] * 8, dp_f=[36] * 8), None, TH) == []   # above freezing


def test_frost_bar_is_higher_on_clear_calm_nights():
    rc = make_rc([30] * 8, dp_f=[32] * 8, wind_mph=2.0)   # dewpoint only 2 F above RST
    assert len(flags.pavement_frost(rc, make_fc(tcc=90), TH)) == 1      # cloudy: counts
    assert flags.pavement_frost(rc, make_fc(tcc=5), TH) == []           # clear + calm: needs > 3 F
    windy = make_rc([30] * 8, dp_f=[32] * 8, wind_mph=12.0)
    assert len(flags.pavement_frost(windy, make_fc(tcc=5), TH)) == 1    # clear but windy: counts


# ----------------------------------------------------------------- black ice
def test_black_ice_needs_wet_road():
    rst = [40, 38, 35, 33, 31, 30, 29]
    assert flags.black_ice(make_rc(rst, water_mm=0.0), TH) == []
    out = flags.black_ice(make_rc(rst, water_mm=0.4), TH)
    assert len(out) == 1 and out[0]["score"] == 1
    assert out[0]["start"] == flags.iso(START + timedelta(minutes=80))   # first step at or below 32


def test_black_ice_counts_each_crossing():
    rst = [40, 30, 30, 36, 31, 31, 38, 29]
    out = flags.black_ice(make_rc(rst, water_mm=0.3), TH)
    assert [o["score"] for o in out] == [1, 2, 3]


# --------------------------------------------------------------- flash freeze
BIG_DROP = [38, 37, 36, 35, 34, 33.5, 33, 32.5, 32, 31.5]   # 6.5 F over 9 steps (3 h)


def test_flash_freeze_big_fast_drop_with_rain():
    out = flags.flash_freeze(make_rc(BIG_DROP, qp_ra=0.5), TH)
    assert len(out) == 1 and out[0]["score"] == 6.5


def test_flash_freeze_requires_size_and_moisture():
    small = [37, 36.5, 36, 35.5, 35, 34, 33.5, 33, 32.5, 32]       # 5 F drop
    assert flags.flash_freeze(make_rc(small, qp_ra=0.5), TH) == []
    assert flags.flash_freeze(make_rc(BIG_DROP), TH) == []              # dry road, no rain
    assert len(flags.flash_freeze(make_rc(BIG_DROP, water_mm=0.2), TH)) == 1   # wet road is enough


# ------------------------------------------------------- wheel-track glazing
def test_glazing_light_snow_cold_pavement():
    out = flags.wheel_track_glazing(make_rc([15] * 6, qp_sn=0.5), TH)   # 0.5 cm/h = ~0.02 in/h liquid
    assert len(out) == 1
    assert flags.wheel_track_glazing(make_rc([25] * 6, qp_sn=0.5), TH) == []   # too warm (> 20 F)
    assert flags.wheel_track_glazing(make_rc([15] * 6, qp_sn=3.0), TH) == []   # heavy snow, calm: not "light"


def test_glazing_blowing_snow_needs_fresh_snow():
    snow = [1.0] + [0.0] * 5
    assert len(flags.wheel_track_glazing(make_rc([15] * 6, wind_mph=20, qp_sn=snow), TH)) == 1
    assert flags.wheel_track_glazing(make_rc([15] * 6, wind_mph=20, qp_sn=0.0), TH) == []   # no snow around


def test_glazing_only_for_heavy_traffic_sites():
    rc = make_rc([15] * 6, qp_sn=0.5)
    heavy = flags.evaluate_site({"id": "a", "station_type": "road", "traffic": "heavy"}, rc, None, {}, TH)
    normal = flags.evaluate_site({"id": "a", "station_type": "road"}, rc, None, {}, TH)
    assert any(f["flag"] == "wheel_track_glazing" for f in heavy["flags"])
    assert "wheel_track_glazing" in normal["not_evaluated"]


# -------------------------------------------------------------- bridges first
def test_bridges_first_with_offset_time_grids():
    bridge = make_rc([30] * 6)
    road = make_rc([36] * 6, start=START + timedelta(minutes=7))   # different minute grid
    out = flags.bridges_first(bridge, road, "road1", TH)
    assert len(out) == 1 and out[0]["score"] == 6.0
    assert flags.bridges_first(bridge, make_rc([30] * 6), "road1", TH) == []   # road also frozen


def test_bridges_first_not_evaluated_without_pair():
    rc = make_rc([30] * 6)
    res = flags.evaluate_site({"id": "b", "station_type": "bridge"}, rc, None, {}, TH)
    assert "bridges_first" in res["not_evaluated"]


# ------------------------------------------------------------ salt / freezing
def test_salt_struggles():
    assert len(flags.salt_struggles(make_rc([10] * 4, rc=3), TH)) == 1
    assert flags.salt_struggles(make_rc([10] * 4, rc=1), TH) == []     # dry road
    assert flags.salt_struggles(make_rc([20] * 4, rc=3), TH) == []     # not cold enough


def test_freezing_rain():
    rc = make_rc([30] * 6)
    assert len(flags.freezing_rain(rc, make_fc(cfrzr=1.0), TH)) == 1
    assert flags.freezing_rain(rc, make_fc(cfrzr=0.0), TH) == []
    assert flags.freezing_rain(make_rc([34] * 6), make_fc(cfrzr=1.0), TH) == []   # RST above 32


# ---------------------------------------------------------------- end to end
COLS = ["cycle_utc", "roadcast_time_utc", "air_temp_c", "dewpoint_c", "wind_kmh", "snow_cm", "rain_mm",
        "snow_rate", "rain_rate", "cloud_octas", "solar_wm2", "infrared_wm2", "road_temp_c",
        "subsurface_temp_c", "road_condition_code", "road_condition"]


def _write_roadcast(path):
    import csv
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for i in range(4):
            w.writerow({"roadcast_time_utc": flags.iso(START + timedelta(minutes=20 * i)),
                        "road_temp_c": f_to_c(15), "dewpoint_c": "-10", "air_temp_c": "-9",
                        "wind_kmh": "5", "rain_mm": "0", "snow_rate": "0", "rain_rate": "0",
                        "road_condition_code": "3"})


def _run(argv):
    old = sys.argv
    sys.argv = ["flags.py"] + argv
    try:
        return flags.main()
    finally:
        sys.argv = old


def test_main_writes_flags_and_latest_pointer(tmp_path):
    rc_dir, out_dir = tmp_path / "roadcast", tmp_path / "flags"
    rc_dir.mkdir()
    (tmp_path / "sites.yaml").write_text(
        "sites:\n"
        "  - {id: s1, name: One, lat: 40, lon: -83, ohgo_site_id: x, station_type: road,"
        " pavement_layers: [{material: asphalt, thickness_m: 0.1}]}\n"
        "  - {id: s2, name: Two, lat: 40, lon: -83, ohgo_site_id: y, station_type: road,"
        " pavement_layers: [{material: asphalt, thickness_m: 0.1}]}\n")
    _write_roadcast(rc_dir / "2026110512z_s1.csv")          # s2 has no roadcast
    latest = tmp_path / "latest.json"
    argv = ["--sites", str(tmp_path / "sites.yaml"), "--roadcast-dir", str(rc_dir),
            "--forcing-dir", str(tmp_path / "none"), "--out-dir", str(out_dir), "--latest", str(latest)]

    assert _run(argv) == 0
    data = json.loads((out_dir / "2026110512z.json").read_text())
    assert list(data["sites"]) == ["s1"]
    assert "s2" in data["skipped_sites"]
    assert any(f["flag"] == "salt_struggles" for f in data["sites"]["s1"]["flags"])
    assert json.loads(latest.read_text())["cycle"] == "2026110512z"

    # an older cycle must not move the pointer backwards
    (rc_dir / "2026110506z_s1.csv").write_text((rc_dir / "2026110512z_s1.csv").read_text())
    assert _run(argv + ["--cycle", "2026110506z"]) == 0
    assert json.loads(latest.read_text())["cycle"] == "2026110512z"


def test_main_returns_error_without_roadcasts(tmp_path):
    assert _run(["--roadcast-dir", str(tmp_path / "empty"), "--out-dir", str(tmp_path / "o"),
                 "--latest", str(tmp_path / "l.json")]) == 1
