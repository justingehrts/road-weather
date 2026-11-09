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


# ---- sites with no subsurface sensor (about half of the OHGO sites near Columbus) ----
def obs_rows(road_f, sst_f=None, start=CYCLE + timedelta(hours=9)):
    out = []
    for i, r in enumerate(road_f):
        out.append({"obs_time_utc": flags.iso(start + timedelta(hours=i)), "air_temp_f": "40", "dewpoint_f": "35",
                    "wind_avg_mph": "5", "road_temp_f": str(r),
                    "subsurface_temp_f": "" if sst_f is None else str(sst_f), "surface_condition": "Dry"})
    return out


def test_a_road_site_without_a_subsurface_reading_gets_an_estimate_instead_of_being_skipped():
    window = run_metro.select_observations(obs_rows([40, 42, 44, 46, 44, 42]), CYCLE, {"station_type": "road"})
    mean_c = (sum([40, 42, 44, 46, 44, 42]) / 6 - 32) * 5 / 9
    assert all(o["sst"] == pytest.approx(mean_c) and o["sst_estimated"] for o in window)


def test_the_estimate_uses_the_last_24_hours_not_just_the_12_hour_window():
    # 30 hourly readings: cold for the first 12, then warm; the window is the last 12 h, the estimate the last 24 h.
    road = [20] * 12 + [60] * 18
    window = run_metro.select_observations(obs_rows(road), CYCLE, {"station_type": "road"})
    last24 = road[-24:]
    assert window[0]["sst"] == pytest.approx((sum(last24) / 24 - 32) * 5 / 9)
    assert len(window) <= 13


def test_a_real_subsurface_reading_is_never_overwritten():
    window = run_metro.select_observations(obs_rows([40, 42, 44, 46], sst_f=50), CYCLE, {"station_type": "road"})
    assert all(o["sst"] == pytest.approx(10.0) and "sst_estimated" not in o for o in window)


def test_a_bridge_still_uses_air_temperature_not_the_road_mean():
    window = run_metro.select_observations(obs_rows([40, 42, 44, 46]), CYCLE, {"station_type": "bridge"})
    assert all(o["sst"] == pytest.approx((40 - 32) * 5 / 9) and "sst_estimated" not in o for o in window)


def test_no_road_temperatures_still_means_skipped():
    rows = obs_rows([40, 42, 44, 46])
    for r in rows:
        r["road_temp_f"] = ""
    with pytest.raises(run_metro.NotEnoughData):
        run_metro.select_observations(rows, CYCLE, {"station_type": "road"})


# ---- METRo is started from inside the site's work folder, so the paths it gets must be absolute ----
FAKE_METRO = '''
import os, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2])) if False else {}
a = sys.argv[1:]
opts = {}
i = 0
while i < len(a):
    if a[i].startswith("--") and i + 1 < len(a) and not a[i + 1].startswith("--"):
        opts[a[i]] = a[i + 1]
        i += 2
    else:
        i += 1
# Like the real METRo: every input must exist and the log file must be creatable, relative to our own cwd.
for k in ("--input-station", "--input-observation", "--input-forecast"):
    if not os.path.exists(opts[k]):
        sys.exit("fake METRo: cannot find " + k + " " + opts[k] + " (cwd " + os.getcwd() + ")")
open(opts["--log-file"], "w").write("ok")
open(opts["--output-roadcast"], "w").write(
    "<roadcast><prediction><roadcast-time>2026-11-09T18:00Z</roadcast-time><at>1</at><st>0.5</st><rc>1</rc></prediction></roadcast>")
'''


def test_relative_work_dir_still_works_because_paths_given_to_metro_are_absolute(tmp_path, monkeypatch):
    """The failure on GitHub: --work-dir was the default relative '.metro_work' and METRo could not open its log."""
    (tmp_path / "metro" / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "metro" / "usr" / "bin" / "metro").write_text(FAKE_METRO)
    write_sites(tmp_path)
    write_forcing(tmp_path)
    (tmp_path / "forcing" / f"hrrr_{TAG}_s1.csv").write_text(
        "cycle_utc,fxx,valid_time_utc,t2m_k,d2m_k,u10_ms,v10_ms,prate_kgm2s,crain,cfrzr,cicep,csnow,tcc_pct,"
        "dswrf_wm2,dlwrf_wm2,sp_pa,grid_lat,grid_lon,ptype\n" +
        "".join(f"{flags.iso(CYCLE)},{h},{flags.iso(CYCLE + timedelta(hours=h))},275,270,1,1,0,0,0,0,0,50,100,300,98000,40,-83,none\n"
                for h in range(3)))
    (tmp_path / "obs").mkdir()
    with (tmp_path / "obs" / "s1.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["obs_time_utc", "air_temp_f", "dewpoint_f", "wind_avg_mph", "wind_max_mph", "wind_dir",
                    "road_temp_f", "subsurface_temp_f", "surface_condition", "surface_sensor_status"])
        for h in (9, 10, 11):
            w.writerow([flags.iso(CYCLE + timedelta(hours=h)), 40, 35, 5, 8, "N", 38, 45, "Dry", "Dry"])
    monkeypatch.chdir(tmp_path)                      # relative paths below are relative to here
    monkeypatch.setattr(sys, "argv", [
        "run_metro.py", "--sites", "sites.yaml", "--obs-dir", "obs", "--forcing-dir", "forcing",
        "--out-dir", "roadcast", "--work-dir", ".metro_work", "--metro-dir", "metro",
        "--metro-python", sys.executable])
    assert run_metro.main() == 0
    assert (tmp_path / "roadcast" / f"{TAG}_s1.csv").exists()
