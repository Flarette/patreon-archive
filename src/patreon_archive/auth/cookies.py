from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yt_dlp
from yt_dlp.utils import expand_path

from ..errors import PreflightError


@contextmanager
def _temp_jar():
    """A 0600 temp file for a cookie jar, removed on exit."""
    fd, tmp = tempfile.mkstemp(prefix="patreon-archive-", suffix=".cookies.txt")
    try:
        os.close(fd)
        os.chmod(tmp, 0o600)
        yield tmp
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass


@contextmanager
def private_copy(path: str | None):
    """Yield a 0600 copy of the cookies file at `path`, removed on exit.

    yt-dlp writes its jar back to `cookiefile` whenever a YoutubeDL closes. Given
    the user's file, every run would rewrite it; given one shared file, parallel
    downloads truncate it while another loads it. So each instance loads a copy
    of its own, and the user's file is only ever read.

    Yields None when `path` is None.
    """
    if not path:
        yield None
        return
    try:
        # Expanded as yt-dlp expands a cookiefile it is handed.
        data = Path(expand_path(path)).read_bytes()
    except OSError as exc:
        raise PreflightError(f"cannot read cookies file {path}: {exc.strerror or exc}") from exc
    with _temp_jar() as tmp:
        Path(tmp).write_bytes(data)
        yield tmp


@contextmanager
def materialized_jar(browser_spec, cookie_file: str | None):
    """Yield a private cookies.txt path holding the session, extracted exactly once.

    Extracting from the browser is expensive (and on Chromium prompts the
    keychain), so a bulk run must not repeat it per post. A cookies file is
    copied instead, so yt-dlp's write-back lands on the copy, never on the
    user's file. Either way the file is 0600 and removed on exit.

    Yields None when there is nothing to materialize, so callers fall through to
    whatever the config already specifies.
    """
    if cookie_file or not browser_spec:
        with private_copy(cookie_file) as tmp:
            yield tmp
        return

    with _temp_jar() as tmp:
        # Note: cookiefile is NOT passed here. yt-dlp would try to *load* the
        # (empty) file at construction and fail the Netscape format check.
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "cookiesfrombrowser": browser_spec,
        }) as ydl:
            jar = ydl.cookiejar
            jar.get_cookies_for_url("https://www.patreon.com/")
            jar.save(filename=tmp, ignore_discard=True, ignore_expires=True)
        yield tmp
