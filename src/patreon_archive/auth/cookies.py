from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar
from yt_dlp.utils import expand_path

from ..errors import PreflightError


def is_patreon(domain: str | None) -> bool:
    """patreon.com or one of its subdomains; a lookalike such as notpatreon.com is not."""
    host = (domain or "").lstrip(".")
    return host == "patreon.com" or host.endswith(".patreon.com")


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

    yt-dlp writes its jar back to `cookiefile` whenever a YoutubeDL closes
    (YoutubeDL.close -> save_cookies, YoutubeDL.py:1055). Given the user's file,
    every run would rewrite it; given one shared file, parallel downloads truncate
    it while another loads it. So each instance loads a copy of its own, and the
    user's file is only ever read.
    """
    if not path:
        yield None
        return
    try:
        # Expanded as yt-dlp expands a cookiefile it is handed (cookies.py:104).
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
    keychain), so a bulk run must not repeat it per post. A cookies file goes
    through private_copy instead. Either way the file is 0600 and removed on exit.

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


def export_patreon_jar(jar_file: str, out: Path) -> tuple[int, int]:
    """Write patreon.com's cookies from `jar_file` to `out`, mode 0600.

    A browser's jar holds every site's logins, and `out` is carried to another
    device, so nothing else goes in. Returns (kept, left out).
    """
    jar = YoutubeDLCookieJar(jar_file)
    jar.load()
    others = [c for c in jar if not is_patreon(c.domain)]
    for c in others:
        jar.clear(c.domain, c.path, c.name)
    # O_CREAT's mode only applies to a new file; fchmod covers one being replaced.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        jar.save(f)
    return len(jar), len(others)
