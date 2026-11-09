"""OHGO's live API answers in camelCase although its docs show PascalCase. These tests use an answer
shaped like the real one (captured from GitHub Actions on 2026-11-09)."""
import pytest

import fetch_rwis
import list_ohgo_sites as lo
from ohgo_api import normalize_keys

REAL_LIKE = {
    "links": [{"href": "https://publicapi.ohgo.com/api/v1/weather-sensor-sites?page-all=true", "rel": "self"}],
    "lastUpdated": "2026-11-09T15:12:23.4949437Z",
    "accecptedFilters": [{"key": "page-all", "value": "true"}],
    "rejectedFilters": [],
    "totalPageCount": 1, "totalResultCount": 2, "currentResultCount": 2,
    "results": [
        {"links": [{"href": "https://publicapi.ohgo.com/api/v1/weather-sensor-sites/200110", "rel": "self"}],
         "id": "200110", "latitude": 39.9336, "longitude": -82.7896,
         "location": "D05 OH101FS LIC IR70 112.16- IR70 @ SR256", "description": "", "severe": False,
         "condition": "", "averageAirTemperature": "51.1",
         "atmosphericSensors": [{"airTemperature": 51.1, "dewpointTemperature": 40.2, "averageWindSpeed": 6,
                                 "maximumWindSpeed": 11, "windDirection": "NW", "lastUpdate": "2026-11-09T09:55:00"}],
         "surfaceSensors": [{"name": "Pavement 1", "status": "OK", "surfaceTemperature": 55.0,
                             "subSurfaceTemperature": 52.0, "lastUpdate": "2026-11-09T09:55:00"}]},
        {"id": "200001", "latitude": 40.7332, "longitude": -83.8236, "location": "D01 OH002F HAR SR235 16.00- SR235 @ SR309",
         "atmosphericSensors": [], "surfaceSensors": []},
    ],
}


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_normalize_keys_converts_camel_to_pascal_everywhere_and_is_idempotent():
    out = normalize_keys(REAL_LIKE)
    assert "Results" in out and "TotalResultCount" in out
    site = out["Results"][0]
    assert site["Id"] == "200110" and site["Latitude"] == 39.9336
    assert site["AtmosphericSensors"][0]["AirTemperature"] == 51.1
    assert site["SurfaceSensors"][0]["SubSurfaceTemperature"] == 52.0
    assert normalize_keys(out) == out                       # already-PascalCase input is left alone
    assert normalize_keys([{"a": 1}, "x"]) == [{"A": 1}, "x"]


def test_fetch_rwis_reads_a_real_shaped_answer_and_builds_an_observation(monkeypatch):
    monkeypatch.setattr(fetch_rwis.requests, "get", lambda *a, **k: FakeResp(REAL_LIKE))
    results = fetch_rwis.fetch_all("key")
    assert [s["Id"] for s in results] == ["200110", "200001"]
    row = fetch_rwis.build_row(results[0])
    assert row["air_temp_f"] == "51.1" and row["road_temp_f"] == "55" and row["subsurface_temp_f"] == "52"
    assert row["dewpoint_f"] == "40.2" and row["wind_dir"] == "NW"
    assert row["obs_time_utc"].endswith("Z")


def test_fetch_rwis_fails_loudly_when_ohgo_returns_no_sites(monkeypatch):
    monkeypatch.setattr(fetch_rwis.requests, "get", lambda *a, **k: FakeResp({"totalResultCount": 0, "results": []}))
    with pytest.raises(RuntimeError, match="no weather sensor sites"):
        fetch_rwis.fetch_all("key")


def test_site_lister_reads_a_real_shaped_answer(monkeypatch):
    monkeypatch.setattr(lo.requests, "get", lambda *a, **k: FakeResp(REAL_LIKE))
    sites, attempts = lo.fetch_sites("key")
    assert len(sites) == 2 and attempts[0]["count"] == 2
    assert "results" in attempts[0]["keys"]                 # reported as OHGO spelled it
    near = lo.nearby(sites, lo.COLUMBUS, 40)
    assert [s["Id"] for s in near] == ["200110"]            # I-70 at SR256 is about 17 miles from downtown
    text = lo.report(sites, near, 40, attempts)
    assert "`200110`" in text and "D05 OH101FS LIC IR70" in text and "ODOT code" in text
