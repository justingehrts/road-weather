"""sites.yaml must parse and every site must have what the pipeline needs."""
from pathlib import Path

import yaml

import run_metro

SITES = yaml.safe_load((Path(__file__).resolve().parent.parent / "sites.yaml").read_text())["sites"]
REQUIRED = ("id", "name", "lat", "lon", "ohgo_site_id", "station_type", "pavement_layers")


def test_required_fields_and_unique_ids():
    ids = [s["id"] for s in SITES]
    assert len(ids) == len(set(ids))
    for s in SITES:
        for field in REQUIRED:
            assert field in s, f"{s.get('id')}: missing {field}"
        assert s["station_type"] in ("road", "bridge")
        assert -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180
        assert s.get("traffic", "normal") in ("heavy", "normal")


def test_layers_use_materials_metro_accepts():
    for s in SITES:
        assert s["pavement_layers"], f"{s['id']}: no layers"
        for layer in s["pavement_layers"]:
            assert layer["material"].lower() in run_metro.LAYER_ALIASES, f"{s['id']}: {layer['material']}"
            assert layer["thickness_m"] > 0


def test_paired_road_sites_exist_and_are_roads():
    by_id = {s["id"]: s for s in SITES}
    for s in SITES:
        pair = s.get("paired_road_site")
        if pair:
            assert s["station_type"] == "bridge", f"{s['id']}: only bridges can have a paired_road_site"
            assert pair in by_id and by_id[pair]["station_type"] == "road"


def test_every_site_has_a_real_unique_ohgo_id():
    ids = [str(s["ohgo_site_id"]) for s in SITES]
    assert len(ids) == len(set(ids)), "duplicate ohgo_site_id"
    for i in ids:
        assert i.isdigit(), f"ohgo_site_id {i!r} is not a real OHGO id (a number kept as text)"
    assert "200053" not in ids                      # deliberately left out


def test_sites_are_near_columbus():
    for s in SITES:
        assert 39.3 <= s["lat"] <= 40.7 and -84.0 <= s["lon"] <= -82.0, f"{s['id']} is outside the Columbus window"
