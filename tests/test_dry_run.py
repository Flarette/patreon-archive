"""A dry run reports the posts it cannot resolve as a real run does, and records nothing."""

from __future__ import annotations

import json

import pytest
from yt_dlp.utils import DownloadError

COLL = "https://www.patreon.com/collection/55"
PIDS = ["6000", "6001", "6002"]
LOCKED = "ERROR: [patreon] 6001: You do not have access to this post"
BROKEN = "ERROR: [patreon] 6002: HTTP Error 500: Internal Server Error"


@pytest.fixture
def mixed(patreon):
    """Three entries: one resolves, one is not viewable, one fails to extract."""
    patreon.post("6001", "Locked", no_access=True)
    patreon.collection("55", "Mixed", [("6000", "Fine"), ("6001", "Locked"),
                                       ("6002", "Broken")])

    def broken(ydl, info, download):
        raise DownloadError(BROKEN)

    patreon.on_extract["6002"] = broken
    return patreon


def test_parallel_dry_run_reports_unresolved_posts(mixed):
    r = mixed.run(COLL, "-n", "-j", "2", "--agent")

    assert r.rc == 1, r
    events = [json.loads(line) for line in r.out.splitlines()]
    assert sorted((e["index"], e["post_id"], e["outcome"], e["detail"])
                  for e in events if e["event"] == "post_done") == [
        (2, "6001", "no_access", LOCKED),
        (3, "6002", "failed", BROKEN),
    ]
    assert [(e["index"], e["post_id"]) for e in events
            if e["event"] == "post_would_download"] == [(1, "6000")]
    store = mixed.store()
    assert store.summary() == []
    assert [m for pid in PIDS for m in store.media_for(pid)] == []


def test_serial_dry_run_prints_unresolved_posts(mixed):
    r = mixed.run(COLL, "-n")

    assert r.rc == 1, r
    assert f"  no access  [6001] {LOCKED}" in r.err
    assert f"  failed  [6002] {BROKEN}" in r.err
    assert [line for line in r.out.splitlines()
            if line.startswith("would download")] == ["would download: Fine  [6000]"]
