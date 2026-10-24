from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# No date prefix and no [%(id)s] suffix by default -- readable names. The upload
# date lives in the file's own metadata tags (see `embed_metadata`) and in `-l`.
# Two videos can share a title, so `pa_suffix` appends " [<id>]" only when the
# title-only name is already taken; ytdlp_runner sets it, and unset it renders
# empty. `--keep-id` always carries the id instead.
# Posts that belong to a collection drop CREATOR_DIR and sit flat in its folder.
CREATOR_DIR = "%(channel,uploader,extractor)s/"
DEFAULT_OUTTMPL = CREATOR_DIR + "%(title).150B%(pa_suffix|)s.%(ext)s"
OUTTMPL_WITH_ID = CREATOR_DIR + "%(title).150B [%(id)s].%(ext)s"


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
    write_thumbnails: bool = False
    write_infojson: bool = False
    write_subs: bool = False
    embed_metadata: bool = True
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
