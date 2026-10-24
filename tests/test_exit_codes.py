"""D7: a collection run's exit code, and what Ctrl-C leaves behind."""

from __future__ import annotations

import json
import os
import signal
import threading
import time

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


def swallowed(ydl, info, download):
    """Ctrl-C while recording a live stream: yt-dlp keeps what it has and goes on."""
    try:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(1)  # the handler raises well before this ends
    except KeyboardInterrupt:
        pass
    return ydl.process_ie_result(info, download=download)


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


def test_ctrl_c_that_ytdlp_swallows_still_stops_the_run(patreon):
    patreon.collection("44", "Run", ENTRIES)
    patreon.on_extract["7002"] = swallowed

    r = patreon.run(COLL, "-a", "zip")

    assert r.rc == 130, r
    assert patreon.extract_calls == ["7001", "7002"]
    # The interrupted post keeps the outcome yt-dlp reported.
    assert "\nsummary\n  complete         2\n" in r.out
    assert r.files == ["Run/One.mp4", "Run/Two.mp4"]


@pytest.fixture
def sigint_seen():
    """An Event set by a SIGINT handler that then raises as Python's does.

    The run's own handler chains to this one, so a worker can wait until the
    main thread is handling the Ctrl-C.
    """
    seen = threading.Event()

    def handler(signum, frame):
        seen.set()
        signal.default_int_handler(signum, frame)

    previous = signal.signal(signal.SIGINT, handler)
    yield seen
    signal.signal(signal.SIGINT, previous)


def test_posts_that_finish_after_ctrl_c_are_counted(patreon, sigint_seen):
    patreon.collection("44", "Run", ENTRIES[:2])

    def late(ydl, info, download):
        # Finishes only once the main thread is handling the Ctrl-C.
        assert sigint_seen.wait(10)
        return ydl.process_ie_result(info, download=download)

    def stop(ydl, info, download):
        os.kill(os.getpid(), signal.SIGINT)
        return ydl.process_ie_result(info, download=download)

    patreon.on_extract["7001"] = late
    patreon.on_extract["7002"] = stop

    r = patreon.run(COLL, "-j", "2", "-a", "zip", "--agent")

    assert r.rc == 130, r
    events = [json.loads(line) for line in r.out.splitlines()]
    assert [(e["summary"], e["exit_code"]) for e in events
            if e["event"] == "run_done"] == [({"complete": 2}, 130)]
    assert r.files == ["Run/One.mp4", "Run/Two.mp4"]
