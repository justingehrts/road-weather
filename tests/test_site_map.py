"""The site map: statuses, click handling and the figure (no browser needed)."""
import json
from pathlib import Path

import pytest

import road_data as rd
import site_map as sm

COORDS = {
    "a": {"name": "Calm Rd", "lat": 40.0, "lon": -83.0, "station_type": "road"},
    "b": {"name": "Frosty Rd", "lat": 40.1, "lon": -83.1, "station_type": "road"},
    "c": {"name": "Busy Rd", "lat": 39.9, "lon": -82.9, "station_type": "bridge"},
    "d": {"name": "Skipped Rd", "lat": 39.8, "lon": -82.8, "station_type": "road"},
}


def flag(name, start, end):
    return {"flag": name.lower(), "name": name, "start": start, "end": end, "hours": 3.0, "score": None, "detail": ""}


FORECAST = {
    "a": {"name": "Calm Rd", "station_type": "road", "flags": []},
    "b": {"name": "Frosty Rd", "station_type": "road",
          "flags": [flag("Pavement frost", "2026-11-18T08:00:00Z", "2026-11-18T12:00:00Z"),
                    flag("Pavement frost", "2026-11-19T08:00:00Z", "2026-11-19T12:00:00Z")]},
    "c": {"name": "Busy Rd", "station_type": "bridge",
          "flags": [flag("Black ice / refreeze", "2026-11-18T08:00:00Z", "2026-11-18T12:00:00Z"),
                    flag("Flash freeze", "2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z")]},
}


def points():
    return {p["id"]: p for p in sm.site_points(COORDS, FORECAST, {"d": "no road readings"})}


def test_status_by_number_of_flag_types():
    p = points()
    assert p["a"]["status"] == "ok"
    assert p["b"]["status"] == "flag"          # two intervals, but only one TYPE of flag
    assert p["c"]["status"] == "multi"
    assert p["d"]["status"] == "none"
    assert "no road readings" in p["d"]["details"][0]
    assert any("Black ice" in line for line in p["c"]["details"])


def test_site_without_coordinates_is_left_off_the_map():
    assert {p["id"] for p in sm.site_points({"a": COORDS["a"]}, {**FORECAST, "zzz": FORECAST["a"]})} == {"a"}


@pytest.mark.parametrize("event, expected", [
    ({"selection": {"points": [{"customdata": "b"}]}}, "b"),
    ({"selection": {"points": [{"customdata": ["c"]}]}}, "c"),      # arrives as a list in some versions
    ({"selection": {"points": []}}, None),
    ({"selection": {"points": [{"customdata": "unknown"}]}}, None),
    ({"selection": {"points": [{"x": 1, "y": 2}]}}, None),          # a click on a line or a no-forecast marker
    (None, None),
    ({}, None),
])
def test_click_to_site(event, expected):
    assert sm.click_to_site(event, {"a", "b", "c"}) == expected


def test_figure_has_one_marker_trace_per_status_and_only_forecast_sites_are_clickable():
    layers = sm.load_layers()
    fig = sm.build_map(list(points().values()), layers, "b", "light")
    markers = {t.name.rsplit(" (", 1)[0]: t for t in fig.data if t.mode == "markers" and t.name}
    assert set(markers) == {label for _, label, _ in sm.STATUSES}
    assert list(markers["No hazard flags"].customdata) == ["a"]
    assert list(markers["2+ types of flags"].customdata) == ["c"]
    assert markers["No forecast this run"].customdata is None
    assert {t.marker.symbol for t in markers.values()} == {"circle", "triangle-up", "diamond", "x"}   # shape, not color alone
    halo = [t for t in fig.data if t.mode == "markers" and not t.name]
    assert len(halo) == 1 and halo[0].x[0] == points()["b"]["x"]                       # ring around the chosen site
    assert fig.layout.legend.itemclick is False                                        # legend is a key, not a filter


def test_each_road_class_is_drawn_differently():
    fig = sm.build_map(list(points().values()), sm.load_layers(), None, "light")
    lines = {t.name: t.line for t in fig.data if t.mode == "lines" and t.name}
    assert set(lines) == {"County line", "State highway", "US highway", "Interstate"}
    assert len({(l.color, l.width, l.dash) for l in lines.values()}) == 4
    assert lines["Interstate"].width > lines["US highway"].width > lines["State highway"].width


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_figure_builds_in_every_theme_and_size(theme, compact):
    fig = sm.build_map(list(points().values()), sm.load_layers(), "a", theme, compact)
    assert fig.layout.dragmode is False and fig.layout.yaxis.scaleanchor == "x"


def test_map_still_draws_sites_when_the_layer_file_is_missing():
    fig = sm.build_map(list(points().values()), None, "a", "light")
    assert sum(len(t.x) for t in fig.data if t.mode == "markers" and t.name and t.x[0] is not None) == 4


def test_labels_do_not_sit_on_sites():
    layers = sm.load_layers()
    real = sm.site_points(rd.load_site_coords(), {}, {})
    fig = sm.build_map(real, layers, None, "light")
    for ann in fig.layout.annotations:
        for p in real:
            assert ((ann.x - p["x"]) ** 2 + (ann.y - p["y"]) ** 2) ** 0.5 > 0.04, (ann.text, p["name"])


# ---- the shipped layer file and the site list the map relies on
def test_layer_file_has_all_four_line_classes_and_stays_small():
    path = Path(sm.LAYERS_FILE)
    data = json.loads(path.read_text())
    assert path.stat().st_size < 400_000                       # it is sent to every visitor's browser
    assert len(data["counties"]) > 10
    assert {r["ref"] for r in data["roads"]["interstate"]} >= {"I-70", "I-71", "I-270"}
    assert any(r["ref"] == "US 33" for r in data["roads"]["us"])
    assert len(data["roads"]["state"]) > 20


def test_every_real_site_has_coordinates_inside_the_map_window():
    coords = rd.load_site_coords()
    assert len(coords) >= 13
    w = sm.load_layers()["window"]
    for sid, c in coords.items():
        assert w["west"] < c["lon"] < w["east"] and w["south"] < c["lat"] < w["north"], sid


def test_missing_sites_file_means_no_map_not_a_crash(tmp_path):
    assert rd.load_site_coords(tmp_path / "nope.yaml") == {}
