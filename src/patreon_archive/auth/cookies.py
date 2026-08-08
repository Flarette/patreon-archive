from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yt_dlp


@contextmanager
def materialized_jar(browser_spec, cookie_file: str | None):
    """Yield a cookies.txt path holding the session, extracted exactly once.

    Extracting from the browser is expensive (and on Chromium prompts the
    keychain), so a bulk run must not repeat it per post. The file is written
    0600 and removed on exit.

    Yields None when there is nothing to materialize, so callers fall through to
    whatever the config already specifies.
    """
    if cookie_file or not browser_spec:
        yield cookie_file
        return

    fd, tmp = tempfile.mkstemp(prefix="patreon-archive-", suffix=".cookies.txt")
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
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
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
