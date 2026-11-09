#!/usr/bin/env python3
"""Poll OHGO for a long stretch of time and keep the observations on the `data` branch.

Why this exists: GitHub starts scheduled workflows hours late (or skips them), and METRo needs a steady
trickle of observations. Instead of relying on an hourly schedule, one job stays running for about 5.5
hours, asks OHGO for new readings every few minutes, and commits them to the data branch once an hour.
(Public repositories get free runner minutes, and the job spends almost all of its time asleep.)

  * Every poll goes through fetch_rwis.collect(), so rows are cleaned and de-duplicated exactly as before.
  * One failed request does not stop the run; many in a row do (so a bad key fails fast, not forever).
  * A stop request (SIGTERM/SIGINT, e.g. a cancelled run) ends the loop and commits what was collected.
  * Commits retry with a rebase. data/obs/*.csv are merged with git's "union" driver (see .gitattributes
    below), so two writers appending different rows to the same file both keep their rows.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import fetch_rwis

MAX_CONSECUTIVE_FAILURES = 8          # at the default 15 minute interval, about two hours of failures
PUSH_ATTEMPTS = 5
GITATTRIBUTES = "data/obs/*.csv merge=union\n"
BOT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *BOT, *args], capture_output=True, text=True, check=check)


def ensure_union_merge(repo: Path) -> None:
    """Make sure the data branch keeps both sides' new rows when two writers append to one CSV."""
    path = repo / ".gitattributes"
    if not path.exists() or GITATTRIBUTES.strip() not in path.read_text():
        path.write_text((path.read_text() if path.exists() else "") + GITATTRIBUTES)


def commit_and_push(repo: Path, message: str, branch: str = "data") -> bool:
    """Commit data/obs (and .gitattributes) if anything changed, then push, rebasing if the push is rejected.

    Returns True if there is nothing left unpushed.
    """
    ensure_union_merge(repo)
    if (repo / "data" / "obs").exists():
        git(repo, "add", "data/obs")
    git(repo, "add", ".gitattributes")
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(repo, "commit", "-q", "-m", message)
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        if git(repo, "push", "origin", branch, check=False).returncode == 0:
            return True
        log(f"push rejected (attempt {attempt}); pulling and retrying")
        pull = git(repo, "pull", "--rebase", "origin", branch, check=False)
        if pull.returncode != 0:
            log("rebase failed: " + (pull.stderr or pull.stdout).strip()[-300:])
            git(repo, "rebase", "--abort", check=False)
        time.sleep(attempt * 3)
    return False


class Stop:
    """Set by SIGTERM/SIGINT so the loop can finish cleanly."""
    requested = False

    @classmethod
    def handler(cls, signum, frame):
        cls.requested = True
        log(f"stop requested (signal {signum}); finishing up")


def interruptible_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not Stop.requested and (left := end - time.monotonic()) > 0:
        time.sleep(min(5.0, left))


def poll(api_key: str, wanted: dict[str, str], repo: Path, duration_min: float, interval_min: float,
         commit_every_min: float, *, collect=fetch_rwis.collect, sleep=interruptible_sleep,
         now=time.monotonic, publish=commit_and_push) -> dict:
    """Run the polling loop. Returns counters. Raises after too many consecutive failed polls."""
    out_dir = repo / "data" / "obs"
    start = now()
    end = start + duration_min * 60
    last_commit = start
    polls = rows = failures = 0
    while True:
        try:
            added = collect(api_key, wanted, out_dir, verbose=False)
            failures = 0
            rows += added
            polls += 1
            log(f"poll {polls}: {added} new row(s), {rows} so far")
        except Exception as exc:                        # one bad request must not end a 5 hour run
            failures += 1
            log(f"poll failed ({failures} in a row): {type(exc).__name__}: {exc}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                publish(repo, "Add RWIS observations (poller stopped after repeated failures)")
                raise RuntimeError(f"{failures} polls failed in a row; giving up") from exc
        if now() - last_commit >= commit_every_min * 60:
            publish(repo, f"Add RWIS observations (poller) {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}")
            last_commit = now()
        if Stop.requested or now() + interval_min * 60 > end:
            break
        sleep(interval_min * 60)
    if not publish(repo, f"Add RWIS observations (poller, final) {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}"):
        raise RuntimeError("could not push the final observations to the data branch")
    return {"polls": polls, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", type=Path, default=Path("sites.yaml"))
    ap.add_argument("--repo", type=Path, required=True, help="checkout of the data branch")
    ap.add_argument("--duration-minutes", type=float, default=325)
    ap.add_argument("--interval-minutes", type=float, default=15)
    ap.add_argument("--commit-every-minutes", type=float, default=60)
    args = ap.parse_args()

    api_key = os.environ.get("OHGO_API_KEY", "").strip()
    if not api_key:
        log("OHGO_API_KEY is not set")
        return 1
    wanted = fetch_rwis.load_sites(args.sites)
    if not wanted:
        log("no sites with a real ohgo_site_id in sites.yaml; nothing to poll")
        return 1

    signal.signal(signal.SIGTERM, Stop.handler)
    signal.signal(signal.SIGINT, Stop.handler)
    log(f"polling {len(wanted)} site(s) every {args.interval_minutes:g} min for {args.duration_minutes:g} min")
    result = poll(api_key, wanted, args.repo, args.duration_minutes, args.interval_minutes, args.commit_every_minutes)
    log(f"finished: {result['polls']} poll(s), {result['rows']} new row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
