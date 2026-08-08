from __future__ import annotations

import shutil
from dataclasses import dataclass

import yt_dlp

from ..config import Config
from ..errors import PreflightError, SessionDeadError

REQUIRED_COOKIE = "session_id"
# AlexCSDev's validator treats this as a hard requirement alongside session_id.
# Not verified against a live session yet, so it is a warning here, not a failure.
DEVICE_COOKIE = "patreon_device_id"


def find_firefox_profiles_with_session() -> list[str]:
    """Profile directory names whose cookie DB holds a Patreon session_id.

    Firefox commonly has several profiles and the one yt-dlp picks by default is
    not necessarily the one the user is logged in with. Reads names only.
    """
    import sqlite3
    from pathlib import Path

    root = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    if not root.is_dir():
        return []
    found = []
    for prof in sorted(root.iterdir()):
        db = prof / "cookies.sqlite"
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            try:
                row = con.execute(
                    "SELECT 1 FROM moz_cookies WHERE host LIKE '%patreon.com' "
                    "AND name = ? LIMIT 1",
                    (REQUIRED_COOKIE,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if row:
            found.append(prof.name)
    return found


@dataclass
class Preflight:
    ffmpeg: str
    cookie_count: int
    has_session: bool
    has_device_id: bool
    warnings: list[str]
    browser_spec: tuple[str, str | None, str | None, str | None] | None = None


def _jar_names(spec, cookie_file) -> tuple[set[str], int]:
    params = {"quiet": True, "no_warnings": True}
    if spec:
        params["cookiesfrombrowser"] = spec
    if cookie_file:
        params["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(params) as ydl:
        jar = ydl.cookiejar
        # yt-dlp loads the jar lazily; touching it forces extraction.
        jar.get_cookies_for_url("https://www.patreon.com/")
        names = {c.name for c in jar if "patreon.com" in (c.domain or "")}
        return names, sum(1 for _ in jar)


def check(cfg: Config) -> Preflight:
    """Fail loudly before any network work.

    A missing ffmpeg otherwise degrades silently into format-selection failures
    rather than an error anyone can act on.
    """
    ffmpeg = cfg.ffmpeg_location or shutil.which("ffmpeg")
    if not ffmpeg:
        raise PreflightError(
            "ffmpeg not found on PATH. HLS muxing needs it; install it or pass --ffmpeg-location."
        )

    if not cfg.browser_spec and not cfg.cookie_file:
        raise PreflightError("No cookie source. Pass --browser firefox or --cookies-file FILE.")

    warnings: list[str] = []
    spec = cfg.browser_spec
    names, total = _jar_names(spec, cfg.cookie_file)

    # Auto-recover: the profile yt-dlp picks by default is frequently not the one
    # the user is logged in with. Rather than fail, scan the other profiles and
    # switch to one that has a live session.
    if REQUIRED_COOKIE not in names and spec and spec[0] == "firefox":
        for prof in find_firefox_profiles_with_session():
            if prof == spec[1]:
                continue
            cand = ("firefox", prof, spec[2], spec[3])
            cand_names, cand_total = _jar_names(cand, cfg.cookie_file)
            if REQUIRED_COOKIE in cand_names:
                warnings.append(
                    f"no Patreon session in firefox profile "
                    f"'{spec[1] or '(default)'}'; using '{prof}' instead"
                )
                spec, names, total = cand, cand_names, cand_total
                break

    has_session = REQUIRED_COOKIE in names
    has_device = DEVICE_COOKIE in names
    if not has_session:
        # Failing loudly matters more than it looks: yt-dlp falls back to
        # anonymous extraction when session_id is absent. On a gated post that
        # errors, but it can also yield a preview stream that looks like success.
        raise SessionDeadError(
            f"No '{REQUIRED_COOKIE}' cookie for patreon.com in any profile. "
            "Log in to Patreon in that browser, then retry."
        )
    if not has_device:
        warnings.append(f"'{DEVICE_COOKIE}' cookie absent; some endpoints may reject the session.")
    if "__cf_bm" not in names:
        warnings.append("'__cf_bm' cookie absent; if requests are challenged, reload patreon.com in the browser.")

    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        # Optional: only the Vimeo embed fallback needs it. Native Patreon HLS
        # does not, so this is a warning rather than a failure -- which is what
        # makes the tool installable under Termux.
        warnings.append(
            "curl_cffi not installed; Vimeo embeds may fail. "
            "Native Patreon video is unaffected. Install with: pip install 'patreon-archive[vimeo]'"
        )

    return Preflight(
        ffmpeg=ffmpeg,
        cookie_count=total,
        has_session=has_session,
        has_device_id=has_device,
        warnings=warnings,
        browser_spec=spec,
    )
