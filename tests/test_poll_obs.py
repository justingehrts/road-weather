"""Tests for scripts/poll_obs.py: timing, failure handling, cancellation and the git side (real git, local remotes)."""
import subprocess
from pathlib import Path

import pytest

import fetch_rwis
import poll_obs
from test_ohgo_api import REAL_LIKE, FakeResp


class Clock:
    """A fake clock: sleeping just moves time forward."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def reset_stop():
    poll_obs.Stop.requested = False
    yield
    poll_obs.Stop.requested = False


def run_poll(tmp_path, clock, collect, publish, duration=60, interval=15, commit_every=30):
    return poll_obs.poll("key", {"1": "s1"}, tmp_path, duration, interval, commit_every,
                         collect=collect, sleep=clock.sleep, now=clock.now, publish=publish)


# ------------------------------------------------------------------ the loop
def test_polls_every_interval_and_commits_on_schedule_and_at_the_end(tmp_path):
    clock, commits, polled_at = Clock(), [], []

    def collect(key, wanted, out, verbose=True):
        polled_at.append(clock.t / 60)
        return 1

    def publish(repo, message):
        commits.append(clock.t / 60)
        return True

    result = run_poll(tmp_path, clock, collect, publish)
    assert polled_at == [0, 15, 30, 45, 60]                 # a poll every 15 minutes for the full hour
    assert commits == [30, 60, 60]                           # every 30 minutes, then once more at the end
    assert result == {"polls": 5, "rows": 5}


def test_a_few_failed_polls_do_not_stop_the_run(tmp_path):
    clock, calls = Clock(), {"n": 0}

    def collect(key, wanted, out, verbose=True):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise ConnectionError("OHGO hiccup")
        return 1

    result = run_poll(tmp_path, clock, collect, lambda *a: True)
    assert result["polls"] == 3 and calls["n"] == 5          # the two failures are skipped, the run continues


def test_too_many_failures_in_a_row_stops_the_run_so_a_bad_key_fails_fast(tmp_path):
    clock, published = Clock(), []

    def collect(key, wanted, out, verbose=True):
        raise PermissionError("OHGO rejected the key")

    with pytest.raises(RuntimeError, match="giving up"):
        run_poll(tmp_path, clock, collect, lambda repo, msg: published.append(msg) or True, duration=600)
    assert "repeated failures" in published[-1]               # whatever was collected is still saved before giving up
    assert clock.t / 60 == 15 * (poll_obs.MAX_CONSECUTIVE_FAILURES - 1)


def test_a_stop_request_ends_the_loop_and_still_commits(tmp_path):
    clock, commits = Clock(), []

    def collect(key, wanted, out, verbose=True):
        if clock.t >= 15 * 60:
            poll_obs.Stop.requested = True                   # e.g. the run was cancelled
        return 1

    result = run_poll(tmp_path, clock, collect, lambda *a: commits.append(1) or True, duration=600)
    assert result["polls"] == 2 and len(commits) >= 1


def test_failing_to_push_the_final_commit_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="final observations"):
        run_poll(tmp_path, Clock(), lambda *a, **k: 0, lambda *a: False, duration=0)


# ------------------------------------------------------------------- collect
def test_collect_adds_new_rows_once_and_skips_repeats(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_rwis.requests, "get", lambda *a, **k: FakeResp(REAL_LIKE))
    wanted = {"200110": "rwis_200110"}
    assert fetch_rwis.collect("k", wanted, tmp_path, verbose=False) == 1
    assert fetch_rwis.collect("k", wanted, tmp_path, verbose=False) == 0      # same reading: nothing new
    assert len((tmp_path / "rwis_200110.csv").read_text().splitlines()) == 2   # header + one row


# ------------------------------------------------------------------ real git
def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


def make_remote_with_obs_file(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "data", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    sh(seed, "checkout", "-q", "-b", "data")
    (seed / "data" / "obs").mkdir(parents=True)
    (seed / "data" / "obs" / "s.csv").write_text("time,value\n")
    poll_obs.ensure_union_merge(seed)
    sh(seed, "add", "-A")
    sh(seed, "commit", "-q", "-m", "init")
    sh(seed, "push", "-q", "origin", "data")
    clones = []
    for name in ("a", "b"):
        c = tmp_path / name
        subprocess.run(["git", "clone", "-q", "-b", "data", str(remote), str(c)], check=True, capture_output=True)
        clones.append(c)
    return remote, clones


def test_two_writers_appending_to_the_same_csv_both_keep_their_rows(tmp_path, monkeypatch):
    """The poller and another workflow (or a manual run) can write data/obs/s.csv at the same time."""
    monkeypatch.setattr(poll_obs.time, "sleep", lambda s: None)
    remote, (a, b) = make_remote_with_obs_file(tmp_path)
    with (a / "data/obs/s.csv").open("a") as f:
        f.write("10:00,1\n")
    assert poll_obs.commit_and_push(a, "a")
    with (b / "data/obs/s.csv").open("a") as f:
        f.write("10:05,2\n")
    assert poll_obs.commit_and_push(b, "b")                  # rejected once, rebased, pushed
    rows = (b / "data/obs/s.csv").read_text().splitlines()
    assert rows[0] == "time,value" and sorted(rows[1:]) == ["10:00,1", "10:05,2"]
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "data", str(remote), str(check)], check=True, capture_output=True)
    assert sorted((check / "data/obs/s.csv").read_text().splitlines()[1:]) == ["10:00,1", "10:05,2"]


def test_the_same_reading_written_by_both_is_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(poll_obs.time, "sleep", lambda s: None)
    remote, (a, b) = make_remote_with_obs_file(tmp_path)
    for c in (a, b):
        with (c / "data/obs/s.csv").open("a") as f:
            f.write("10:00,1\n")
    assert poll_obs.commit_and_push(a, "a") and poll_obs.commit_and_push(b, "b")
    assert (b / "data/obs/s.csv").read_text().splitlines() == ["time,value", "10:00,1"]


def test_commit_and_push_with_nothing_new_is_a_clean_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(poll_obs.time, "sleep", lambda s: None)
    remote, (a, b) = make_remote_with_obs_file(tmp_path)
    before = sh(a, "rev-parse", "HEAD")
    assert poll_obs.commit_and_push(a, "nothing")
    assert sh(a, "rev-parse", "HEAD") == before


def test_a_brand_new_data_branch_gets_the_merge_setting_in_its_first_commit(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "data", str(remote)], check=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "data", str(repo)], check=True)
    sh(repo, "remote", "add", "origin", str(remote))
    (repo / "data" / "obs").mkdir(parents=True)
    (repo / "data" / "obs" / "s.csv").write_text("time,value\n10:00,1\n")
    assert poll_obs.commit_and_push(repo, "first")
    assert "merge=union" in (repo / ".gitattributes").read_text()
    assert "data/obs/s.csv" in sh(repo, "ls-tree", "-r", "--name-only", "HEAD")
