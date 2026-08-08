from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# No [%(id)s] suffix by default -- readable names. `--keep-id` restores it, which
# is worth doing for a bulk archive: two posts can share a title, and without the
# id the second silently overwrites the first.
DEFAULT_OUTTMPL = "%(channel,uploader,extractor)s/%(upload_date>%Y-%m-%d,epoch>%Y-%m-%d)s - %(title).150B.%(ext)s"
OUTTMPL_WITH_ID = "%(channel,uploader,extractor)s/%(upload_date>%Y-%m-%d,epoch>%Y-%m-%d)s - %(title).150B [%(id)s].%(ext)s"


@dataclass(frozen=True)
class Config:
    archive_root: Path
    temp_dir: Path
    state_db: Path
    browser_spec: tuple[str, str | None, str | None, str | None] | None = None
    cookie_file: str | None = None
    outtmpl: str = DEFAULT_OUTTMPL
    fmt: str = "bestvideo*+bestaudio/best"
    ffmpeg_location: str | None = None
    frag_concurrency: int = 4
    request_sleep: float = 0.0
    http_retries: int = 10
    extractor_retries: int = 3
    write_thumbnails: bool = True
    write_infojson: bool = True
    write_subs: bool = True
    quiet: bool = False
    force: bool = False
    concurrency: int = 1
    agent: bool = False
    extra_ytdlp: dict = field(default_factory=dict)


def parse_browser_spec(spec: str | None) -> tuple[str, str | None, str | None, str | None] | None:
    """`firefox`, `chrome:Profile 1`, `firefox::keyring`, `chrome:Default::container`.

    Mirrors yt-dlp's --cookies-from-browser 4-tuple (browser, profile, keyring, container).
    """
    if not spec:
        return None
    parts = spec.split(":")
    browser = parts[0].strip().lower()
    profile = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    keyring = parts[2].strip().upper() if len(parts) > 2 and parts[2].strip() else None
    container = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
    return (browser, profile, keyring, container)
