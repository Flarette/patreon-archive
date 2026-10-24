"""C2, D6: the user's cookies file is only ever read; each download loads its own copy."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

COLL = "https://www.patreon.com/collection/88"
ENTRIES = [("8001", "One"), ("8002", "Two"), ("8003", "Three"),
           ("8004", "Four"), ("8005", "Five"), ("8006", "Six")]
FILES = sorted(f"Batch/{title}.mp4" for _, title in ENTRIES)
# A fixed past mtime, so a rewrite shows even within one tick of the clock.
OLD_NS = 1_000_000_000 * 10**9


@pytest.fixture
def jars(patreon) -> dict[str, tuple[Path, int]]:
    """Six posts; each download notes the cookie file its yt-dlp loads, and that file's mode."""
    patreon.collection("88", "Batch", ENTRIES)
    # Readable by others, so a copy that kept the source's mode would show.
    os.chmod(patreon.cookies, 0o644)
    os.utime(patreon.cookies, ns=(OLD_NS, OLD_NS))
    seen: dict[str, tuple[Path, int]] = {}

    def note_jar(ydl, info, download):
        jar = Path(ydl.params["cookiefile"])
        seen[info["id"]] = (jar, stat.S_IMODE(jar.stat().st_mode))
        return ydl.process_ie_result(info, download=download)

    for pid, _ in ENTRIES:
        patreon.on_extract[pid] = note_jar
    return seen


@pytest.mark.parametrize("args", [["-j", "3"], []], ids=["parallel", "serial"])
def test_run_never_writes_the_cookies_file(patreon, jars, args):
    before = patreon.cookies.read_bytes()

    r = patreon.run(COLL, *args)

    assert r.rc == 0, r
    assert r.files == FILES
    assert patreon.cookies.read_bytes() == before
    assert patreon.cookies.stat().st_mtime_ns == OLD_NS
    assert sorted(jars) == [pid for pid, _ in ENTRIES]
    copies = {jar for jar, _ in jars.values()}
    assert len(copies) == len(ENTRIES)
    assert patreon.cookies not in copies
    assert {mode for _, mode in jars.values()} == {0o600}
    assert list(Path(tempfile.gettempdir()).glob("patreon-archive-*")) == []


def test_missing_cookies_file_fails_preflight_and_is_not_created(patreon):
    patreon.post("8101", "Lone")
    patreon.cookies.unlink()

    r = patreon.run("https://www.patreon.com/posts/8101")

    assert r.rc == 4, r
    assert str(patreon.cookies) in r.err
    assert not patreon.cookies.exists()
    assert patreon.extract_calls == []
