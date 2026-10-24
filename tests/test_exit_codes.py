"""A collection run's exit code, and what Ctrl-C leaves behind."""

from __future__ import annotations

import concurrent.futures as cf
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


def signalled(ydl, info, download):
    os.kill(os.getpid(), signal.SIGINT)
    return ydl.process_ie_result(info, download=download)


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
    """no_access sets the code only when nothing completed: a selection that mixes
    in a few locked posts still did its job. Locked here means viewable to the
    listing and refused at extraction, as with an expired session.
    """
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
    """An interrupted run is not the set the user asked to pack."""
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
        assert sigint_seen.wait(10)
        return ydl.process_ie_result(info, download=download)

    patreon.on_extract["7001"] = late
    patreon.on_extract["7002"] = signalled

    r = patreon.run(COLL, "-j", "2", "-a", "zip", "--agent")

    assert r.rc == 130, r
    events = [json.loads(line) for line in r.out.splitlines()]
    assert [(e["summary"], e["exit_code"]) for e in events
            if e["event"] == "run_done"] == [({"complete": 2}, 130)]
    assert r.files == ["Run/One.mp4", "Run/Two.mp4"]


def test_ctrl_c_before_the_last_submit_cancels_the_queued_posts(patreon, monkeypatch):
    """Ctrl-C lands while the third post sits queued and the fourth is unsubmitted.

    The running posts are held until the pool's shutdown has run, so whether the
    queued post was cancelled does not depend on timing.
    """
    patreon.collection("44", "Run", ENTRIES + [("7004", "Four")])
    queued = threading.Event()
    shut = threading.Event()
    submitted: list[cf.Future] = []
    submit = cf.ThreadPoolExecutor.submit
    shutdown = cf.ThreadPoolExecutor.shutdown

    def submit_then_wait(self, fn, /, *args, **kwargs):
        future = submit(self, fn, *args, **kwargs)
        submitted.append(future)
        if len(submitted) == 3:
            queued.set()
            time.sleep(10)  # the handler raises well before this ends
        return future

    def shutdown_then_release(self, wait=True, *, cancel_futures=False):
        shutdown(self, wait=False, cancel_futures=cancel_futures)
        shut.set()
        shutdown(self, wait=wait, cancel_futures=cancel_futures)

    def first(ydl, info, download):
        assert queued.wait(10)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
        assert shut.wait(10)
        return ydl.process_ie_result(info, download=download)

    def second(ydl, info, download):
        assert shut.wait(10)
        return ydl.process_ie_result(info, download=download)

    monkeypatch.setattr(cf.ThreadPoolExecutor, "submit", submit_then_wait)
    monkeypatch.setattr(cf.ThreadPoolExecutor, "shutdown", shutdown_then_release)
    patreon.on_extract["7001"] = first
    patreon.on_extract["7002"] = second

    r = patreon.run(COLL, "-j", "2")

    assert r.rc == 130, r
    assert sorted(patreon.extract_calls) == ["7001", "7002"]
    assert r.files == ["Run/One.mp4", "Run/Two.mp4"]


@pytest.fixture
def sigint_restored():
    """Puts back whatever SIGINT handler the test started with."""
    previous = signal.getsignal(signal.SIGINT)
    yield previous
    signal.signal(signal.SIGINT, previous)


def test_ignored_ctrl_c_stays_ignored(patreon, sigint_restored):
    """As in a job a script puts in the background."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    patreon.collection("44", "Run", ENTRIES)
    patreon.on_extract["7002"] = signalled

    r = patreon.run(COLL)

    assert r.rc == 0, r
    assert r.files == ["Run/One.mp4", "Run/Three.mp4", "Run/Two.mp4"]
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN


def test_ctrl_c_just_after_the_handler_goes_in_still_restores_it(patreon, sigint_restored,
                                                                 monkeypatch):
    patreon.collection("44", "Run", ENTRIES)
    install = signal.signal
    landed = []

    def install_then_interrupt(signum, handler):
        previous = install(signum, handler)
        if not landed:
            landed.append(handler)
            raise KeyboardInterrupt
        return previous

    monkeypatch.setattr(signal, "signal", install_then_interrupt)

    r = patreon.run(COLL)

    assert r.rc == 130, r
    assert len(landed) == 1
    assert signal.getsignal(signal.SIGINT) is sigint_restored
    assert patreon.extract_calls == []


def test_collection_runs_off_the_main_thread(patreon, sigint_restored):
    """Only the main thread may set a signal handler; elsewhere SIGINT is left alone."""
    patreon.collection("44", "Run", ENTRIES[:1])
    runs = []
    worker = threading.Thread(target=lambda: runs.append(patreon.run(COLL)))

    worker.start()
    worker.join(30)

    assert [r.rc for r in runs] == [0], runs
    assert signal.getsignal(signal.SIGINT) is sigint_restored
