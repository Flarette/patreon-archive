"""D7: a collection run's exit code, and what Ctrl-C leaves behind."""

from __future__ import annotations

import json

import pytest
from yt_dlp.utils import DownloadError

COLL = "https://www.patreon.com/collection/44"
ENTRIES = [("7001", "One"), ("7002", "Two"), ("7003", "Three")]


def post_url(pid: str) -> str:
    return f"https://www.patreon.com/posts/{pid}"


def broken(ydl, info, download):
    raise DownloadError(f"ERROR: [patreon] {info['id']}: HTTP Error 500")


def interrupt(ydl, info, download):
    raise KeyboardInterrupt


@pytest.mark.parametrize(("locked", "fails", "jobs", "rc"), [
    (["7001", "7002"], [], "1", 2),
    (["7001", "7002"], [], "2", 2),
    (["7002"], [], "1", 0),
    (["7002"], ["7001"], "1", 1),
], ids=["no_access-serial", "no_access-parallel", "complete+no_access",
        "failed+no_access"])
def test_collection_exit_code(patreon, locked, fails, jobs, rc):
    # Viewable to the listing, refused at extraction: an expired session.
    for pid in locked:
        patreon.post(pid, dict(ENTRIES)[pid], no_access=True)
    for pid in fails:
        patreon.on_extract[pid] = broken
    patreon.collection("44", "Mixed", ENTRIES[:2])

    r = patreon.run(COLL, "-j", jobs)

    assert r.rc == rc, r
    assert sorted(patreon.extract_calls) == ["7001", "7002"]


@pytest.fixture
def stopped(patreon):
    """Three posts; Ctrl-C lands while the second one is extracting."""
    patreon.collection("44", "Run", ENTRIES)
    patreon.on_extract["7002"] = interrupt
    return patreon


def test_interrupted_serial_run_stops_and_skips_the_archive(stopped):
    r = stopped.run(COLL, "-a", "zip")

    assert r.rc == 130, r
    assert stopped.extract_calls == ["7001", "7002"]
    assert "\nsummary\n  complete         1\n" in r.out
    assert r.files == ["Run/One.mp4"]


def test_interrupted_parallel_run_skips_the_archive(stopped):
    r = stopped.run(COLL, "-j", "2", "-a", "zip")

    assert r.rc == 130, r
    assert "\nsummary\n" in r.out
    # Which posts finish while in-flight work drains is a race; that One is
    # among them, and that nothing gets packed, is not.
    assert "Run/One.mp4" in r.files
    assert not [f for f in r.files if f.endswith(".zip")]


@pytest.mark.parametrize("jobs", ["1", "2"])
def test_interrupted_run_reports_130_in_run_done(stopped, jobs):
    r = stopped.run(COLL, "-j", jobs, "--agent")

    assert r.rc == 130, r
    events = [json.loads(line) for line in r.out.splitlines()]
    assert [e["exit_code"] for e in events if e["event"] == "run_done"] == [130]
    assert "\nsummary\n" in r.err


def test_interrupted_single_post_exits_130(patreon):
    patreon.post("7004", "Solo")
    patreon.on_extract["7004"] = interrupt

    r = patreon.run(post_url("7004"))

    assert r.rc == 130, r
