from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_VIMEO = re.compile(
    r"^https?://(?:www\.|player\.)?vimeo\.com/(?:video/)?(?P<id>\d+)(?:/(?P<h>[\da-f]+))?"
)
_TEN_HEX = re.compile(r"[\da-f]{10}")


def normalize_vimeo_embed(embed_url: str) -> str | None:
    """Rewrite a Patreon Vimeo embed into the form yt-dlp's fast path can read.

    PatreonIE re-emits the unlisted hash as a query parameter (`?h=<hash>`), but
    VimeoIE._VALID_URL captures `unlisted_hash` only in *path* position and only
    as exactly ten hex characters. The query form therefore never reaches
    `_extract_from_api` and falls back to a slower, more failure-prone path.

    Because we enumerate ourselves we own `embed.url`, so we can hand yt-dlp the
    path form directly.

    Returns None when the URL is not Vimeo. When a hash is present but is not
    exactly ten hex chars, the query form is preserved: the path form would then
    fail _VALID_URL outright and fall through to the Generic extractor, which is
    strictly worse.
    """
    m = _VIMEO.match(embed_url)
    if not m:
        return None
    vid = m.group("id")
    h = m.group("h") or (parse_qs(urlsplit(embed_url).query).get("h") or [None])[0]
    if h and _TEN_HEX.fullmatch(h):
        return f"https://player.vimeo.com/video/{vid}/{h}"
    if h:
        return f"https://player.vimeo.com/video/{vid}?h={h}"
    return f"https://player.vimeo.com/video/{vid}"
