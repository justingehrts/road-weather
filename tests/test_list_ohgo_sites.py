"""Tests for scripts/list_ohgo_sites.py using a made-up OHGO response (no network, no key)."""
import sys

import pytest
import yaml

import list_ohgo_sites as lo

SITES = [
    {"Id": "55-1", "Latitude": 39.9667, "Longitude": -83.2000, "Location": "I-70 West of Columbus",
     "Description": "Near Hilliard", "AverageAirTemperature": "41.2",
     "AtmosphericSensors": [{"AirTemperature": 41.2, "DewpointTemperature": 30.0, "LastUpdate": "2026-11-09T10:00:00"}],
     "SurfaceSensors": [{"Name": "Pavement 1", "Status": "OK", "SurfaceTemperature": 44.1,
                         "SubSurfaceTemperature": 48.0, "SurfaceCondition": "Dry", "LastUpdate": "2026-11-09T10:00:00"}]},
    {"Id": "88-3", "Latitude": 40.1000, "Longitude": -83.0200, "Location": "I-270 Outerbelt Bridge over Scioto",
     "Description": "Bridge deck sensor", "AtmosphericSensors": [], "SurfaceSensors": []},
    {"Id": "99-9", "Latitude": 41.4993, "Longitude": -81.6944, "Location": "I-90 Cleveland", "Description": "far away"},
    {"Id": "no-coords", "Location": "broken record"},
]


def test_distance_is_sane():
    assert lo.miles_between(*lo.COLUMBUS, *lo.COLUMBUS) == 0
    assert 120 < lo.miles_between(*lo.COLUMBUS, 41.4993, -81.6944) < 130      # Columbus to Cleveland


def test_nearby_filters_sorts_and_skips_records_without_coordinates():
    near = lo.nearby(SITES, lo.COLUMBUS, 40)
    assert [s["Id"] for s in near] == ["88-3", "55-1"]       # about 9.7 miles, then about 10.6 miles
    assert [s["miles"] for s in near] == sorted(s["miles"] for s in near)
    assert "99-9" not in [s["Id"] for s in near] and "no-coords" not in [s["Id"] for s in near]
    assert len(lo.nearby(SITES, lo.COLUMBUS, 200)) == 3


def test_station_type_guess_looks_for_bridge_words():
    assert lo.guess_station_type(SITES[1]) == "bridge"
    assert lo.guess_station_type(SITES[0]) == "road"


def test_sensor_fields_shows_a_surface_condition_if_one_exists():
    fields = lo.sensor_fields(SITES)
    assert "SurfaceCondition" in fields["SurfaceSensors"]
    assert "AirTemperature" in fields["AtmosphericSensors"]


def test_yaml_snippet_is_valid_and_carries_the_ohgo_id():
    near = lo.nearby(SITES, lo.COLUMBUS, 40)
    data = yaml.safe_load(lo.yaml_snippet(near))
    assert {s["ohgo_site_id"] for s in data["sites"]} == {"55-1", "88-3"}
    by_id = {s["ohgo_site_id"]: s for s in data["sites"]}
    assert by_id["88-3"]["station_type"] == "bridge"
    assert by_id["55-1"]["lat"] == pytest.approx(39.9667)
    assert all(s["id"].startswith("rwis_") and s["pavement_layers"] for s in data["sites"])


def test_report_has_table_snippet_fields_and_a_full_record():
    near = lo.nearby(SITES, lo.COLUMBUS, 40)
    text = lo.report(SITES, near, 40)
    assert "`55-1`" in text and "I-70 West of Columbus" in text
    assert "ohgo_site_id" in text and "SurfaceCondition" in text
    assert "One site's record" in text and '"Id"' in text


def test_missing_key_is_reported_not_crashed(monkeypatch, capsys):
    monkeypatch.delenv("OHGO_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["list_ohgo_sites.py"])
    assert lo.main() == 1
    assert "OHGO_API_KEY is not set" in capsys.readouterr().err


def test_main_never_prints_the_key(monkeypatch, capsys, tmp_path):
    secret = "super-secret-key-value"
    monkeypatch.setenv("OHGO_API_KEY", secret)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(lo, "fetch_sites", lambda key: (SITES, [{"label": "all pages", "params": {}, "status": 200, "keys": ["Results"], "meta": {}, "preview": "", "count": len(SITES)}]))
    monkeypatch.setattr(sys, "argv", ["list_ohgo_sites.py", "--radius-miles", "40"])
    assert lo.main() == 0
    out = capsys.readouterr()
    assert secret not in out.out + out.err and secret not in summary.read_text()
    assert "55-1" in summary.read_text()


def test_a_rejected_key_gives_a_plain_message(monkeypatch):
    monkeypatch.setattr(lo.requests, "get", lambda *a, **k: FakeResp(401))
    with pytest.raises(SystemExit, match="rejected the API key"):
        lo.fetch_sites("bad")


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_an_empty_answer_is_explained_not_hidden(monkeypatch):
    """What happened on GitHub: HTTP 200 but no sites. The report must say what OHGO actually sent."""
    answer = {"Links": [], "TotalResultCount": 0, "RejectedFilters": [{"Name": "page-all", "Message": "not valid"}], "Results": []}
    monkeypatch.setattr(lo.requests, "get", lambda *a, **k: FakeResp(200, answer))
    sites, attempts = lo.fetch_sites("k")
    assert sites == [] and len(attempts) == len(lo.ATTEMPTS)          # every way of asking was tried
    text = lo.report(sites, [], 40, attempts)
    assert "What OHGO answered" in text and "RejectedFilters" in text and "page-all" in text
    assert "TotalResultCount" in text


def test_a_later_way_of_asking_is_used_if_the_first_returns_nothing(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        return FakeResp(200, {"Results": SITES} if params == {} else {"Results": []})
    monkeypatch.setattr(lo.requests, "get", fake_get)
    sites, attempts = lo.fetch_sites("k")
    assert len(sites) == len(SITES) and [a["count"] for a in attempts] == [0, len(SITES)]
    assert calls == [{"page-all": "true"}, {}]


def test_non_json_answers_do_not_crash(monkeypatch):
    monkeypatch.setattr(lo.requests, "get", lambda *a, **k: FakeResp(200, None, text="<html>oops</html>"))
    sites, attempts = lo.fetch_sites("k")
    assert sites == [] and "oops" in attempts[0]["preview"]


def test_sites_without_a_pavement_sensor_are_marked_and_left_out_of_the_snippet():
    near = lo.nearby(SITES, lo.COLUMBUS, 40)            # 88-3 has no sensors; 55-1 has a pavement sensor
    assert lo.pavement_temp(SITES[0]) == 44.1 and lo.pavement_temp(SITES[1]) is None
    text = lo.report(SITES, near, 40)
    assert "1 of these 2 sites have a pavement sensor reporting a real temperature" in text
    assert "**no sensors**" in text and "Left out (no pavement reading right now): `88-3`" in text
    snippet = text.split("```yaml")[1].split("```")[0]
    assert "55-1" in snippet and "88-3" not in snippet


def test_pavement_temp_ignores_garbage_readings():
    assert lo.pavement_temp({"SurfaceSensors": [{"SurfaceTemperature": -148.0}, {"SurfaceTemperature": 40.5}]}) == 40.5
    assert lo.pavement_temp({"SurfaceSensors": [{"SurfaceTemperature": 9999}]}) is None
    assert lo.pavement_temp({"SurfaceSensors": []}) is None


# OHGO fills unused sensor slots with -9999999: a site can list nine sensors and report nothing.
EMPTY_SLOTS = {"Id": "38007", "Latitude": 39.8919, "Longitude": -83.04, "Location": "IR71 S @ IR270",
               "SurfaceSensors": [{"Name": "", "Status": "Unknown", "SurfaceTemperature": -9999999,
                                   "SubSurfaceTemperature": 32.0, "LastUpdate": "2026-11-09T10:05:02"}]
               + [{"Name": "", "Status": "Unknown", "SurfaceTemperature": -9999999, "SubSurfaceTemperature": -9999999}] * 8}


def test_empty_sensor_slots_do_not_count_as_working_sensors():
    assert len(lo.pavement_sensors(EMPTY_SLOTS)) == 9
    assert lo.working_pavement_sensors(EMPTY_SLOTS) == [] and lo.pavement_temp(EMPTY_SLOTS) is None
    near = lo.nearby([EMPTY_SLOTS], lo.COLUMBUS, 40)
    text = lo.report([EMPTY_SLOTS], near, 40)
    assert "0 of these 1 sites have a pavement sensor reporting a real temperature" in text and "**0** of 9" in text
    assert "Left out (no pavement reading right now): `38007`" in text
    assert "-9999999" in text                                  # the explanation mentions OHGO's empty marker


def test_the_sample_record_collapses_repeated_empty_slots():
    sample = lo.compact_sample(EMPTY_SLOTS)
    assert len(sample["SurfaceSensors"]) <= 3 and "9 surface sensor slots" in sample["SurfaceSensorsNote"]
    assert "miles" not in sample


def test_subsurface_temp_ignores_the_empty_marker():
    assert lo.subsurface_temp(EMPTY_SLOTS) == 32.0
    assert lo.subsurface_temp({"SurfaceSensors": [{"SubSurfaceTemperature": -9999999}]}) is None
