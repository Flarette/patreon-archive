"""Test harness: real yt-dlp on local media, Patreon stubbed out.

yt-dlp runs for real -- format selection, naming, download, postprocessors and
progress hooks -- against 1-second mp4 files that ffmpeg generates once per
session. Only the two network edges are replaced: the Patreon API (`cli._api`)
and extraction (`YoutubeDL.extract_info`), which hands canned info for a
registered post to yt-dlp's own `process_ie_result`. Nothing here may reach the
network; a socket connect fails the test.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.utils import DownloadError

from patreon_archive import cli, events
from patreon_archive.api.client import ApiError, Collection, Entry
from patreon_archive.state.store import Store

STATE_DB = ".patreon-archive.db"

# One colour and one tone per key, so every file's content (and hash) differs.
_SOURCES = {"a": ("red", 440), "b": ("green", 660), "c": ("blue", 880)}


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.fail("ffmpeg must be on PATH: the suite runs real yt-dlp postprocessors")
    root = tmp_path_factory.mktemp("media").resolve()
    files = {}
    for key, (colour, tone) in _SOURCES.items():
        path = root / f"{key}.mp4"
        # -map_metadata -1: no tags of our own, so a date tag on the output can
        # only have come from FFmpegMetadata.
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=c={colour}:s=64x48:d=1:r=10",
             "-f", "lavfi", "-i", f"sine=frequency={tone}:duration=1",
             "-map_metadata", "-1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
             str(path)],
            check=True,
        )
        files[key] = path
    return files


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Keep every test off the user's environment, terminal and network."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PATREON_ARCHIVE_OUTPUT", raising=False)
    monkeypatch.delenv("PATREON_ARCHIVE_COOKIES", raising=False)
    # The default output root is relative to cwd.
    monkeypatch.chdir(tmp_path)
    # Not a TTY, so the full-collection prompt can never block a run.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(events, "_EMITTER", None)
    # Cookie copies are temp files. gettempdir() caches its answer, so TMPDIR
    # would not reach it; this keeps them, and any leftovers, in the test's dir.
    temp = tmp_path / "tempdir"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))

    def _no_network(self, *args, **kwargs):
        raise OSError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network)


@dataclasses.dataclass
class Run:
    rc: int
    out: str
    err: str
    # Every file under the destination except the state db and .tmp/, as POSIX
    # paths relative to it. Sidecars count, so a stray one shows up here.
    files: list[str]


@dataclasses.dataclass
class FakePost:
    pid: str
    title: str
    media: str = "a"
    channel: str = "Creator"
    upload_date: str = "20240606"
    collections: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    no_access: bool = False
    videos: list[str] | None = None


class _FakeApi:
    """What `cli._api` yields: the two calls the CLI makes."""

    def __init__(self, fake: FakePatreon):
        self.fake = fake

    def collection(self, cid: str) -> Collection:
        if cid not in self.fake.collections:
            raise ApiError(f"Patreon returned 404 for collection/{cid}.")
        title, entries = self.fake.collections[cid]
        built = []
        for pid, entry_title in entries:
            if entry_title is None:
                # Missing from `included`: the API says nothing about it.
                built.append(Entry(post_id=pid))
                continue
            d = self.fake.posts[pid].upload_date
            built.append(Entry(post_id=pid, title=entry_title,
                               published=f"{d[:4]}-{d[4:6]}-{d[6:]}",
                               duration=1.0, can_view=True))
        # Already oldest first: the newest-first reversal lives in PatreonApi.
        return Collection(collection_id=cid, title=title, num_posts=len(entries),
                          post_ids=[pid for pid, _ in entries], entries=built)

    def post_collections(self, pid: str) -> list[tuple[str, str | None]]:
        if pid in self.fake.lookup_fails:
            raise ApiError(f"Patreon returned 404 for posts/{pid}.")
        found: dict[str, str | None] = {}
        post = self.fake.posts.get(pid)
        for cid, title in post.collections if post else ():
            found.setdefault(cid, title)
        for cid, (title, entries) in self.fake.collections.items():
            if any(p == pid for p, _ in entries):
                found.setdefault(cid, title)
        return list(found.items())


class FakePatreon:
    """Registered posts and collections, served to the CLI with no network.

    `on_extract` maps a pid to `fn(ydl, info, download)`, which replaces the
    default extraction for that pid: mutate `info` and return
    `ydl.process_ie_result(info, download=download)`, or raise.
    """

    def __init__(self, root: Path, media: dict[str, Path], capsys):
        self.root = root
        self.out = root / "out"
        self.cookies = root / "cookies.txt"
        self.posts: dict[str, FakePost] = {}
        self.collections: dict[str, tuple[str, list[tuple[str, str | None]]]] = {}
        self.lookup_fails: set[str] = set()
        self.extract_calls: list[str] = []
        self.on_extract: dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._media = media
        self._capsys = capsys
        self._stores: list[Store] = []
        # Enough for the real preflight.check to pass offline.
        self.cookies.write_text("# Netscape HTTP Cookie File\n" + "".join(
            f".patreon.com\tTRUE\t/\tTRUE\t4102444800\t{name}\ttest-{name}\n"
            for name in ("session_id", "patreon_device_id", "__cf_bm")))

    def post(self, pid, title, *, media="a", channel="Creator", upload_date="20240606",
             collections=(), no_access=False, videos=None) -> FakePost:
        post = FakePost(pid=str(pid), title=title, media=media, channel=channel,
                        upload_date=upload_date, collections=list(collections),
                        no_access=no_access,
                        videos=list(videos) if videos is not None else None)
        self.posts[post.pid] = post
        return post

    def collection(self, cid, title, entries) -> None:
        """`entries` oldest first, as (pid, title); a None title is a blocked entry.

        Entries not registered with post() are registered here, blocked ones as
        no_access.
        """
        entries = [(str(pid), t) for pid, t in entries]
        for pid, t in entries:
            if pid not in self.posts:
                self.post(pid, t or f"Post {pid}", no_access=t is None)
        self.collections[str(cid)] = (title, entries)

    def run(self, *args: str) -> Run:
        args = list(args)
        ours, passthrough = args, []
        if "--" in args:
            i = args.index("--")
            ours, passthrough = args[:i], args[i + 1:]
        argv = [*ours, "-o", str(self.out), "--cookies-file", str(self.cookies),
                "--", *passthrough, "--enable-file-urls"]
        self._capsys.readouterr()
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        cap = self._capsys.readouterr()
        return Run(rc=rc, out=cap.out, err=cap.err, files=self._files())

    def store(self) -> Store:
        store = Store(self.out / STATE_DB)
        self._stores.append(store)
        return store

    def close(self) -> None:
        for store in self._stores:
            store.close()

    def _files(self) -> list[str]:
        if not self.out.exists():
            return []
        found = []
        for p in self.out.rglob("*"):
            rel = p.relative_to(self.out)
            if not p.is_file() or rel.parts[0] == ".tmp":
                continue
            if len(rel.parts) == 1 and rel.name.startswith(STATE_DB):
                continue
            found.append(rel.as_posix())
        return sorted(found)

    def _info(self, post: FakePost, url: str) -> dict:
        """Shaped like yt-dlp's Patreon extractor output for the post."""
        common = {
            "title": post.title,
            "channel": post.channel,
            "upload_date": post.upload_date,
            "description": f"About {post.title}",
            "extractor": "patreon",
            "extractor_key": "Patreon",
            "webpage_url": url,
            "original_url": url,
        }

        def video(vid: str, key: str) -> dict:
            return {"id": vid, "ext": "mp4", "url": self._media[key].as_uri(), **common}

        if post.videos is None:
            return video(post.pid, post.media)
        return {"_type": "playlist", "id": post.pid, **common,
                "entries": [video(f"{post.pid}-{n}", key)
                            for n, key in enumerate(post.videos, 1)]}

    def _extract(self, ydl, url: str, download: bool = True):
        pid = cli.extract_post_id(url) or url
        with self._lock:
            self.extract_calls.append(pid)
        post = self.posts.get(pid)
        if post is None:
            raise DownloadError(f"ERROR: [patreon] {pid}: not registered with FakePatreon")
        info = self._info(post, url)
        if pid in self.on_extract:
            return self.on_extract[pid](ydl, info, download)
        if post.no_access:
            raise DownloadError(f"ERROR: [patreon] {pid}: You do not have access to this post")
        return ydl.process_ie_result(info, download=download)


@pytest.fixture
def patreon(tmp_path, media, capsys, monkeypatch):
    fake = FakePatreon(tmp_path.resolve(), media, capsys)

    @contextlib.contextmanager
    def fake_api(cfg):
        yield _FakeApi(fake)

    def extract_info(self, url, download=True, **kwargs):
        return fake._extract(self, url, download)

    monkeypatch.setattr(cli, "_api", fake_api)
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    yield fake
    fake.close()
