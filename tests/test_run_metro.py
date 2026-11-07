"""Exit-code behaviour of scripts/run_metro.py and scripts/flags.py when there is little or no data."""
import csv
import sys
from datetime import datetime, timedelta, timezone

import pytest

import flags
import run_metro

TAG = "2026110706z"
CYCLE = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)


def write_sites(tmp_path, material="asphalt"):
    (tmp_path / "sites.yaml").write_text(
        "sites:\n"
        f"  - {{id: s1, name: One, lat: 40, lon: -83, ohgo_site_id: x, station_type: road,"
        f" pavement_layers: [{{material: {material}, thickness_m: 0.1}}]}}\n")


def write_forcing(tmp_path):
    d = tmp_path / "forcing"
    d.mkdir()
    (d / f"hrrr_{TAG}_s1.csv").write_text("cycle_utc,fxx,valid_time_utc\n")


def run_metro_main(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_metro.py", "--sites", str(tmp_path / "sites.yaml"), "--obs-dir", str(tmp_path / "obs"),
        "--forcing-dir", str(tmp_path / "forcing"), "--out-dir", str(tmp_path / "roadcast"),
        "--work-dir", str(tmp_path / "work"), "--metro-dir", str(tmp_path / "no-metro-needed")])
    return run_metro.main()


def test_all_sites_skipped_for_lack_of_observations_is_a_warning_not_a_failure(tmp_path, monkeypatch, capsys):
    """The 08:00 UTC run on GitHub: forcing present, no observations collected yet."""
    write_sites(tmp_path)
    write_forcing(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert run_metro_main(tmp_path, monkeypatch) == 0
    err = capsys.readouterr().err
    assert "skipped - no observations yet" in err
    assert "::warning::METRo produced no roadcasts" in err      # shows as a yellow warning on GitHub


def test_a_real_problem_still_fails_the_run(tmp_path, monkeypatch):
    write_sites(tmp_path, material="steel")                     # METRo has no steel layer type
    write_forcing(tmp_path)
    (tmp_path / "obs").mkdir()
    with (tmp_path / "obs" / "s1.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["obs_time_utc", "air_temp_f", "dewpoint_f", "wind_avg_mph", "wind_max_mph", "wind_dir",
                    "road_temp_f", "subsurface_temp_f", "surface_condition", "surface_sensor_status"])
        for h in (9, 10, 11):                                    # hourly, after the 06Z cycle
            w.writerow([flags.iso(CYCLE + timedelta(hours=h)), 40, 35, 5, 8, "N", 38, 45, "", "OK"])
    assert run_metro_main(tmp_path, monkeypatch) == 1


def test_no_forcing_at_all_still_fails(tmp_path, monkeypatch):
    write_sites(tmp_path)
    (tmp_path / "forcing").mkdir()
    assert run_metro_main(tmp_path, monkeypatch) == 1


def test_flags_with_no_roadcasts_for_the_cycle_is_a_clean_noop(tmp_path, monkeypatch, capsys):
    write_sites(tmp_path)
    (tmp_path / "roadcast").mkdir()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "argv", [
        "flags.py", "--cycle", TAG, "--sites", str(tmp_path / "sites.yaml"),
        "--roadcast-dir", str(tmp_path / "roadcast"), "--forcing-dir", str(tmp_path / "forcing"),
        "--out-dir", str(tmp_path / "flags"), "--latest", str(tmp_path / "latest.json")])
    assert flags.main() == 0
    assert "::warning::No hazard flags" in capsys.readouterr().err
    assert not (tmp_path / "latest.json").exists()               # nothing published, pointer untouched
    assert not (tmp_path / "flags").exists()
