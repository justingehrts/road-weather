"""Tests for scripts/verify.py with small hand-checkable examples."""
import csv
import json
import math
import sys
from datetime import datetime, timedelta, timezone

import pytest

import flags
import verify

CYCLE = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TAG = "2026110512z"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 5, 15, 20, tzinfo=timezone.utc)      # first roadcast step


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def t(h, m=0):
    return datetime(2026, 11, 5, h, m, tzinfo=timezone.utc)


def write_roadcast(root, site, rst_f=40.0, air_f=35.0, steps=13, tag=TAG, start=T0):
    d = root / "roadcast"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{tag}_{site}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["roadcast_time_utc", "road_temp_c", "air_temp_c"])
        w.writeheader()
        for i in range(steps):
            w.writerow({"roadcast_time_utc": flags.iso(start + timedelta(minutes=20 * i)),
                        "road_temp_c": f_to_c(rst_f), "air_temp_c": f_to_c(air_f)})


def write_obs(root, site, rows):
    """rows: (datetime, rst_f, air_f, condition)"""
    d = root / "obs"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{site}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["obs_time_utc", "air_temp_f", "road_temp_f", "surface_condition"])
        for when, rst, air, cond in rows:
            w.writerow([flags.iso(when), air, rst, cond])


def write_flags(root, site_flags, tag=TAG):
    d = root / "flags"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tag}.json").write_text(json.dumps({"sites": site_flags}))


def site(sid, kind="road", **extra):
    return {"id": sid, "name": sid.upper(), "station_type": kind, **extra}


def run(root, sites, days=30):
    return verify.verify(sites, root / "roadcast", root / "flags", root / "obs", NOW, days)


# ------------------------------------------------------------ temperature
def test_bias_and_rmse_for_known_errors(tmp_path):
    write_roadcast(tmp_path, "s1", rst_f=40.0, air_f=35.0)
    write_obs(tmp_path, "s1", [
        (t(14, 20), 99, 99, ""),          # before the roadcast started: must be ignored
        (t(15, 20), 38, 34, ""),          # forecast 40 -> error +2
        (t(16, 20), 41, 36, ""),          # -1
        (t(17, 20), 43, 33, ""),          # -3
        (t(23, 0), 99, 99, ""),           # after the roadcast ended: must be ignored
    ])
    res = run(tmp_path, [site("s1")])
    assert res["matched_pairs"] == 3 and res["cycles_verified"] == 1
    road = res["temperature"]["road_temp_f"]["by_site"]["s1"]["overall"]
    assert road["n"] == 3
    assert road["bias"] == round(-2 / 3, 2)
    assert road["rmse"] == round(math.sqrt(14 / 3), 2)
    assert road["mae"] == 2.0
    air = res["temperature"]["air_temp_f"]["by_site"]["s1"]["overall"]   # forecast 35 vs 34, 36, 33
    assert air["bias"] == round((1 - 1 + 2) / 3, 2)


def test_errors_are_grouped_by_lead_time_since_cycle(tmp_path):
    write_roadcast(tmp_path, "s1", rst_f=40.0)
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, ""),      # 3.3 h after 12Z  -> 0-6
                               (t(19, 20), 44, 35, "")])     # 7.3 h            -> 6-12
    res = run(tmp_path, [site("s1")])
    leads = {r["lead"]: r for r in res["temperature"]["road_temp_f"]["by_site"]["s1"]["by_lead"]}
    assert set(leads) == {"0-6", "6-12"}
    assert leads["0-6"]["bias"] == 0.0
    assert leads["6-12"]["bias"] == -4.0


def test_forecast_is_interpolated_between_steps(tmp_path):
    d = tmp_path / "roadcast"
    d.mkdir()
    with (d / f"{TAG}_s1.csv").open("w", newline="") as f:      # 40 F at 15:20, 50 F at 15:40
        w = csv.DictWriter(f, fieldnames=["roadcast_time_utc", "road_temp_c", "air_temp_c"])
        w.writeheader()
        for i, v in enumerate((40.0, 50.0)):
            w.writerow({"roadcast_time_utc": flags.iso(T0 + timedelta(minutes=20 * i)),
                        "road_temp_c": f_to_c(v), "air_temp_c": f_to_c(v)})
    write_obs(tmp_path, "s1", [(t(15, 30), 45, 45, "")])        # halfway: forecast 45
    res = run(tmp_path, [site("s1")])
    assert res["temperature"]["road_temp_f"]["by_site"]["s1"]["overall"]["bias"] == 0.0


def test_old_cycles_outside_window_are_skipped(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, "")])
    assert run(tmp_path, [site("s1")], days=30)["matched_pairs"] == 1
    assert run(tmp_path, [site("s1")], days=0)["matched_pairs"] == 0     # cycle is a day old


def test_nothing_to_verify_gives_empty_but_valid_summary(tmp_path):
    res = run(tmp_path, [site("s1")])
    assert res["matched_pairs"] == 0
    assert res["temperature"]["road_temp_f"]["by_site"] == {}
    assert all(f["overall"] is None for f in res["flags"]["by_flag"].values())


# -------------------------------------------------------------- conditions
@pytest.mark.parametrize("text,expected", [
    ("Dry", {"dry"}), ("Wet", {"wet"}), ("Chemically Wet", {"wet"}), ("Frost", {"frost"}),
    ("Ice Warning", {"ice"}), ("Black Ice", {"ice"}), ("Snow Warning", {"snow"}),
    ("Ice Watch", set()),            # "watch" = possible, not present
    ("Slush", {"snow", "wet"}),
])
def test_condition_vocabulary(text, expected):
    assert verify.classify_condition(text) == expected


def test_blank_condition_is_unknown_not_dry():
    assert verify.classify_condition("") is None
    assert verify.classify_condition(None) is None


# -------------------------------------------------------------------- flags
def test_contingency_counts_and_rates(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [
        (t(15, 20), 40, 35, "Frost"),     # flagged, frost seen        -> hit
        (t(15, 40), 40, 35, "Dry"),       # flagged, dry               -> false alarm
        (t(16, 20), 40, 35, "Frost"),     # not flagged, frost seen    -> miss
        (t(17, 20), 40, 35, "Dry"),       # not flagged, dry           -> correct negative
    ])
    write_flags(tmp_path, {"s1": {"evaluated": ["pavement_frost", "black_ice"], "flags": [
        {"flag": "pavement_frost", "start": flags.iso(t(15, 20)), "end": flags.iso(t(16, 0))}]}})
    res = run(tmp_path, [site("s1")])

    pf = res["flags"]["by_flag"]["pavement_frost"]["overall"]
    assert (pf["hits"], pf["misses"], pf["false_alarms"], pf["correct_negatives"]) == (1, 1, 1, 1)
    assert pf["hit_rate"] == 0.5
    assert pf["false_alarm_ratio"] == 0.5      # 1 of the 2 flagged times had no frost
    assert pf["false_alarm_rate"] == 0.5       # 1 of the 2 frost-free times was flagged
    assert pf["low_sample"] is True

    bi = res["flags"]["by_flag"]["black_ice"]["overall"]    # never observed, never flagged
    assert bi["correct_negatives"] == 4 and bi["hit_rate"] is None and bi["false_alarm_ratio"] is None
    assert bi["false_alarm_rate"] == 0.0


def test_flag_not_evaluated_for_a_site_is_not_scored(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, "Ice Warning")])
    write_flags(tmp_path, {"s1": {"evaluated": ["pavement_frost"], "flags": []}})
    res = run(tmp_path, [site("s1")])
    assert res["flags"]["by_flag"]["black_ice"]["overall"] is None


def test_observations_without_condition_are_skipped_and_reported(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, ""), (t(16, 20), 40, 35, "")])
    write_flags(tmp_path, {"s1": {"evaluated": ["pavement_frost"], "flags": []}})
    res = run(tmp_path, [site("s1")])
    assert res["flags"]["by_flag"]["pavement_frost"]["overall"] is None
    assert res["flags"]["condition_data"] == {"observations": 2, "with_surface_condition": 0}
    assert res["matched_pairs"] == 2        # temperature verification does not need conditions


def test_flash_freeze_needs_wet_then_ice(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, "Wet"), (t(16, 20), 30, 30, "Ice Warning"),
                               (t(17, 20), 30, 30, "Ice Warning")])
    write_flags(tmp_path, {"s1": {"evaluated": ["flash_freeze", "black_ice"], "flags": [
        {"flag": "flash_freeze", "start": flags.iso(t(16, 20)), "end": flags.iso(t(16, 20))}]}})
    res = run(tmp_path, [site("s1")])
    ff = res["flags"]["by_flag"]["flash_freeze"]["overall"]
    # 16:20 ice after wet at 15:20 (1 h earlier): event + flagged = hit.
    # 17:20 ice, but wet was 2 h earlier (inside 3 h) so still an event, and not flagged = miss.
    assert (ff["hits"], ff["misses"]) == (1, 1)
    assert res["flags"]["by_flag"]["black_ice"]["overall"]["observed_events"] == 2


def test_bridges_first_uses_observed_road_temperatures(tmp_path):
    write_roadcast(tmp_path, "b1")
    write_obs(tmp_path, "b1", [(t(15, 20), 30, 30, ""), (t(16, 20), 36, 36, "")])
    write_obs(tmp_path, "r1", [(t(15, 0), 40, 40, ""), (t(17, 0), 40, 40, "")])
    write_flags(tmp_path, {"b1": {"evaluated": ["bridges_first"], "flags": [
        {"flag": "bridges_first", "start": flags.iso(t(15, 20)), "end": flags.iso(t(15, 20))}]}})
    sites = [site("b1", "bridge", paired_road_site="r1"), site("r1")]
    res = run(tmp_path, sites)
    bf = res["flags"]["by_flag"]["bridges_first"]["overall"]
    # 15:20: bridge 30 <= 32 < road 40 -> event, flagged -> hit.  16:20: bridge 36 -> no event, not flagged.
    assert (bf["hits"], bf["correct_negatives"], bf["misses"], bf["false_alarms"]) == (1, 1, 0, 0)


def test_salt_struggles_is_reported_as_not_verifiable(tmp_path):
    res = run(tmp_path, [site("s1")])
    assert any("Salt" in k for k in res["flags"]["not_verified"])


def test_main_writes_summary_file(tmp_path):
    write_roadcast(tmp_path, "s1")
    write_obs(tmp_path, "s1", [(t(15, 20), 40, 35, "")])
    (tmp_path / "sites.yaml").write_text(
        "sites:\n  - {id: s1, name: S1, lat: 40, lon: -83, ohgo_site_id: x, station_type: road,"
        " pavement_layers: [{material: asphalt, thickness_m: 0.1}]}\n")
    out = tmp_path / "verification" / "summary.json"
    old = sys.argv
    sys.argv = ["verify.py", "--sites", str(tmp_path / "sites.yaml"), "--roadcast-dir", str(tmp_path / "roadcast"),
                "--flags-dir", str(tmp_path / "flags"), "--obs-dir", str(tmp_path / "obs"), "--out", str(out),
                "--now", flags.iso(NOW)]
    try:
        assert verify.main() == 0
    finally:
        sys.argv = old
    data = json.loads(out.read_text())
    assert data["matched_pairs"] == 1 and data["lead_bins"][0] == "0-6"
