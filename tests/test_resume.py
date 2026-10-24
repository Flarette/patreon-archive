"""A re-run resumes under -j as it does serially, and a new version reopens nothing."""

from __future__ import annotations

import json

import pytest

from patreon_archive import cli
from patreon_archive.errors import Outcome

COLL = "https://www.patreon.com/collection/66"
ENTRIES = [("5001", "One"), ("5002", "Two"), ("5003", "Three"), ("5004", "Four")]
PIDS = [pid for pid, _ in ENTRIES]
FILES = sorted(f"Shorts/{title}.mp4" for _, title in ENTRIES)


@pytest.fixture
def done(patreon):
    """A 4-post collection a serial run has already downloaded in full."""
    patreon.collection("66", "Shorts", ENTRIES)
    first = patreon.run(COLL)
    assert first.rc == 0, first
    assert first.files == FILES
    patreon.extract_calls.clear()
    return patreon


def test_parallel_rerun_skips_complete_posts(done):
    r = done.run(COLL, "-j", "2", "--agent")

    assert r.rc == 0, r
    assert done.extract_calls == []
    skips = sorted(line for line in r.err.splitlines() if line.startswith("skip "))
    assert skips == sorted(f"skip [{n}] {pid}: already complete"
                           for n, pid in enumerate(PIDS, 1))
    events = [json.loads(line) for line in r.out.splitlines()]
    assert sorted((e["index"], e["post_id"]) for e in events
                  if e["event"] == "post_skipped") == list(enumerate(PIDS, 1))
    assert r.files == FILES


def test_force_reprocesses_under_parallel(done):
    """Each post still owns its recorded name, so nothing gains a suffix."""
    r = done.run(COLL, "-j", "2", "--force")

    assert r.rc == 0, r
    assert sorted(done.extract_calls) == PIDS
    assert r.files == FILES


def test_new_tool_version_alone_reopens_nothing(done, monkeypatch):
    monkeypatch.setattr(cli, "__version__", "99.0.0")

    r = done.run(COLL)

    assert r.rc == 0, r
    assert done.extract_calls == []


@pytest.mark.parametrize("jobs", ["1", "2"])
def test_complete_post_with_its_file_gone_is_fetched_again(done, jobs):
    """The store records the name as this post's, so it comes back unsuffixed."""
    (done.out / "Shorts/Two.mp4").unlink()

    r = done.run(COLL, "-j", jobs)

    assert r.rc == 0, r
    assert done.extract_calls == ["5002"]
    assert r.files == FILES


def test_embed_is_recorded_under_the_requested_post_id(patreon):
    """An embed's extractor id is the provider's, not the post's."""
    url = "https://www.patreon.com/posts/5101"
    patreon.post("5101", "Embedded")

    def as_embed(ydl, info, download):
        info["id"] = "777000111"
        return ydl.process_ie_result(info, download=download)

    patreon.on_extract["5101"] = as_embed
    first = patreon.run(url)
    assert first.rc == 0, first
    store = patreon.store()
    assert store.get_post("5101")["outcome"] == Outcome.COMPLETE
    assert store.get_post("777000111") is None

    r = patreon.run(url)

    assert r.rc == 0, r
    assert "skip 5101: already complete" in r.out
    assert patreon.extract_calls == ["5101"]
