"""Tests for the retry and cycle-choice logic in scripts/fetch_hrrr.py (no network, no Herbie needed)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fetch_hrrr

CYCLE = datetime(2026, 11, 6, 18, 0)


class FlakyHerbie:
    """Stands in for a Herbie object whose download fails a set number of times first."""

    def __init__(self, failures, calls):
        self.failures, self.calls = failures, calls

    def xarray(self, search, overwrite=False, **kw):
        self.calls.append(overwrite)
        if len(self.calls) <= self.failures:
            raise RuntimeError("Processing failed: 503 Server Error: Service Unavailable")
        return "dataset"


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(fetch_hrrr.time, "sleep", waits.append)
    return waits


def test_a_brief_503_is_retried_and_the_run_continues(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(fetch_hrrr, "make_herbie", lambda *a: FlakyHerbie(2, calls))
    assert fetch_hrrr.read_hour(CYCLE, 2, Path("x")) == ["dataset"]
    assert len(calls) == 3
    assert calls == [False, True, True]          # retries re-download rather than trust a half-written file
    assert no_sleep == [5, 10]                   # waits grow: 5 s then 10 s


def test_gives_up_after_all_attempts_so_no_partial_forecast_is_published(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(fetch_hrrr, "make_herbie", lambda *a: FlakyHerbie(99, calls))
    with pytest.raises(RuntimeError, match="503"):
        fetch_hrrr.read_hour(CYCLE, 2, Path("x"))
    assert len(calls) == fetch_hrrr.DOWNLOAD_ATTEMPTS
    assert no_sleep == [5, 10, 20, 40]


def test_no_retry_wait_when_first_attempt_works(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(fetch_hrrr, "make_herbie", lambda *a: FlakyHerbie(0, calls))
    fetch_hrrr.read_hour(CYCLE, 0, Path("x"))
    assert calls == [False] and no_sleep == []


@pytest.mark.parametrize("now,expected", [
    # normal on-time runs: about two hours after the cycle
    (datetime(2026, 11, 6, 20, 10, tzinfo=timezone.utc), datetime(2026, 11, 6, 18, tzinfo=timezone.utc)),
    (datetime(2026, 11, 6, 2, 5, tzinfo=timezone.utc), datetime(2026, 11, 6, 0, tzinfo=timezone.utc)),
    (datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc), datetime(2026, 11, 6, 6, tzinfo=timezone.utc)),
    # a run that GitHub started late still gets the newest cycle that is old enough
    (datetime(2026, 11, 7, 23, 12, tzinfo=timezone.utc), datetime(2026, 11, 7, 18, tzinfo=timezone.utc)),
])
def test_on_time_and_late_runs_pick_the_newest_cycle_old_enough(now, expected):
    assert fetch_hrrr.latest_cycle(now) == expected


@pytest.mark.parametrize("now,expected", [
    # the failure on GitHub: run started 34 min after the 18Z cycle, which cannot be complete for ~2 h
    (datetime(2026, 11, 8, 18, 34, tzinfo=timezone.utc), datetime(2026, 11, 8, 12, tzinfo=timezone.utc)),
    (datetime(2026, 11, 8, 17, 59, tzinfo=timezone.utc), datetime(2026, 11, 8, 12, tzinfo=timezone.utc)),
    (datetime(2026, 11, 8, 19, 59, tzinfo=timezone.utc), datetime(2026, 11, 8, 12, tzinfo=timezone.utc)),
    (datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc), datetime(2026, 11, 8, 18, tzinfo=timezone.utc)),
    # and across midnight
    (datetime(2026, 11, 8, 1, 30, tzinfo=timezone.utc), datetime(2026, 11, 7, 18, tzinfo=timezone.utc)),
])
def test_a_cycle_younger_than_two_hours_is_never_targeted(now, expected):
    assert fetch_hrrr.latest_cycle(now) == expected


def test_only_synoptic_cycles_are_ever_chosen():
    for hour in range(24):
        for minute in (0, 30):
            c = fetch_hrrr.latest_cycle(datetime(2026, 11, 8, hour, minute, tzinfo=timezone.utc))
            assert c.hour in (0, 6, 12, 18) and c.minute == 0
