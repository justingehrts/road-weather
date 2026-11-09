"""Smoke tests for the Verification page, fed by real verify.py output (no network)."""
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import flags
import road_data as rd
import verify
from test_verify import NOW, site, t, write_flags, write_obs, write_roadcast

PAGE = str(Path(__file__).resolve().parent.parent / "app" / "verification.py")


@pytest.fixture(autouse=True)
def fresh_cache():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def publish_summary(root, with_conditions=True):
    """Run verify.py's logic on a tiny dataset and 'publish' the summary like the data branch."""
    cond = (lambda c: c) if with_conditions else (lambda c: "")
    write_roadcast(root, "s1")
    write_obs(root, "s1", [(t(15, 20), 38, 34, cond("Frost")), (t(15, 40), 41, 36, cond("Dry")),
                           (t(16, 20), 43, 33, cond("Frost")), (t(17, 20), 40, 35, cond("Dry"))])
    write_flags(root, {"s1": {"evaluated": ["pavement_frost"], "flags": [
        {"flag": "pavement_frost", "start": flags.iso(t(15, 20)), "end": flags.iso(t(16, 0))}]}})
    result = verify.verify([site("s1")], root / "roadcast", root / "flags", root / "obs", NOW, 30)
    out = root / "data" / "verification"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(result))
    return result


def serve_from(monkeypatch, root):
    monkeypatch.setattr(rd, "fetch_text", lambda path, base=None, timeout=20:
                        (root / path).read_text() if (root / path).exists() else None)


def run_page():
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    return at


def test_page_renders_results(tmp_path, monkeypatch):
    publish_summary(tmp_path)
    serve_from(monkeypatch, tmp_path)
    at = run_page()
    assert not at.exception
    assert at.selectbox[0].value == "__all__"
    assert len(at.table) == 2                                    # by-site table + flag table
    assert not at.warning                                        # conditions present: no warning
    assert "matches" in " ".join(m.value for m in at.markdown)


def test_page_picks_a_site_and_other_measurement(tmp_path, monkeypatch):
    publish_summary(tmp_path)
    serve_from(monkeypatch, tmp_path)
    at = run_page()
    at.selectbox[0].select("s1").run()
    at.radio[0].set_value("air_temp_f").run()
    assert not at.exception


def test_page_explains_missing_surface_conditions(tmp_path, monkeypatch):
    publish_summary(tmp_path, with_conditions=False)
    serve_from(monkeypatch, tmp_path)
    at = run_page()
    assert not at.exception
    assert any("surface-condition" in w.value for w in at.warning)


def test_page_handles_nothing_published_yet(tmp_path, monkeypatch):
    serve_from(monkeypatch, tmp_path)
    at = run_page()
    assert not at.exception
    assert any("No verification results" in i.value for i in at.info)


def test_page_handles_zero_matches(tmp_path, monkeypatch):
    result = verify.verify([site("s1")], tmp_path / "roadcast", tmp_path / "flags", tmp_path / "obs", NOW, 30)
    out = tmp_path / "data" / "verification"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps(result))
    serve_from(monkeypatch, tmp_path)
    at = run_page()
    assert not at.exception
    assert any("no matches yet" in i.value for i in at.info)


def test_page_reports_unreachable_data(monkeypatch):
    def boom(*a, **k):
        raise rd.DataError("no network")
    monkeypatch.setattr(rd, "fetch_text", boom)
    at = run_page()
    assert not at.exception
    assert any("Could not load" in e.value for e in at.error)
