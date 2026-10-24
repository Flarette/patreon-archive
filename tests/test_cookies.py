"""C2, D6: a cookies file is only ever read; each download loads its own copy."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest

from patreon_archive.auth.cookies import private_copy

COLL = "https://www.patreon.com/collection/88"
ENTRIES = [("8001", "One"), ("8002", "Two"), ("8003", "Three"),
           ("8004", "Four"), ("8005", "Five"), ("8006", "Six")]
FILES = sorted(f"Batch/{title}.mp4" for _, title in ENTRIES)
# A fixed past mtime, so a rewrite shows even within one tick of the clock.
OLD_NS = 1_000_000_000 * 10**9
# Only the passthrough file holds this cookie, so a jar that has it came from there.
MARKER = "pa_passthrough"


class Jar(NamedTuple):
    path: Path
    mode: int
    names: frozenset[str]


def _as_users(path: Path) -> None:
    # Readable by others, so a copy that kept the source's mode would show.
    os.chmod(path, 0o644)
    os.utime(path, ns=(OLD_NS, OLD_NS))


def _leftovers() -> list[Path]:
    return list(Path(tempfile.gettempdir()).glob("patreon-archive-*"))


@pytest.fixture
def jars(patreon) -> dict[str, Jar]:
    """Six posts; each download notes the cookie file its yt-dlp loads."""
    patreon.collection("88", "Batch", ENTRIES)
    _as_users(patreon.cookies)
    seen: dict[str, Jar] = {}

    def note_jar(ydl, info, download):
        path = Path(ydl.params["cookiefile"])
        seen[info["id"]] = Jar(path, stat.S_IMODE(path.stat().st_mode),
                               frozenset(c.name for c in ydl.cookiejar))
        return ydl.process_ie_result(info, download=download)

    for pid, _ in ENTRIES:
        patreon.on_extract[pid] = note_jar
    return seen


@pytest.mark.parametrize(("jobs", "passthrough"), [
    ("3", False), ("1", False), ("2", True), ("1", True),
], ids=["parallel", "serial", "passthrough-parallel", "passthrough-serial"])
def test_run_never_writes_a_cookies_file(patreon, jars, jobs, passthrough):
    users = [patreon.cookies]
    args = [COLL, "-j", jobs]
    if passthrough:
        # yt-dlp's own --cookies overrides ours when the params merge, so this
        # is the file each download loads.
        other = patreon.root / "other-cookies.txt"
        other.write_text(patreon.cookies.read_text()
                         + f".patreon.com\tTRUE\t/\tTRUE\t4102444800\t{MARKER}\tx\n")
        _as_users(other)
        users.append(other)
        args += ["--", "--cookies", str(other)]
    before = {p: p.read_bytes() for p in users}

    r = patreon.run(*args)

    assert r.rc == 0, r
    assert r.files == FILES
    assert {p: p.read_bytes() for p in users} == before
    assert [p.stat().st_mtime_ns for p in users] == [OLD_NS] * len(users)
    assert sorted(jars) == [pid for pid, _ in ENTRIES]
    copies = {jar.path for jar in jars.values()}
    assert len(copies) == len(ENTRIES)
    assert not copies & set(users)
    assert {jar.mode for jar in jars.values()} == {0o600}
    assert {MARKER in jar.names for jar in jars.values()} == {passthrough}
    assert _leftovers() == []


def test_missing_cookies_file_fails_preflight_and_is_not_created(patreon):
    patreon.post("8101", "Lone")
    patreon.cookies.unlink()

    r = patreon.run("https://www.patreon.com/posts/8101")

    assert r.rc == 4, r
    assert str(patreon.cookies) in r.err
    assert not patreon.cookies.exists()
    assert patreon.extract_calls == []


def test_a_copy_that_fails_in_setup_leaves_no_temp_file(tmp_path, monkeypatch):
    src = tmp_path / "cookies.txt"
    src.write_text("# Netscape HTTP Cookie File\n")

    def refuse(*args, **kwargs):
        raise PermissionError("chmod refused")

    monkeypatch.setattr(os, "chmod", refuse)
    with pytest.raises(PermissionError), private_copy(str(src)):
        pass

    assert _leftovers() == []
