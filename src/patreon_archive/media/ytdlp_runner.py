from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from ..config import Config
from ..errors import Outcome

YTDLP_VERSION = yt_dlp.version.__version__

# yt-dlp distinguishes these two at patreon.py:504-512 via current_user_can_view.
# We match on its messages because, driving it by post URL, that is the signal we get.
_NO_ACCESS = re.compile(r"do not have access", re.I)
_NO_MEDIA = re.compile(r"no supported media found", re.I)


@dataclass
class Result:
    outcome: Outcome
    detail: str | None = None
    filepath: Path | None = None
    title: str | None = None
    post_id: str | None = None
    uploader: str | None = None
    duplicate_of: Path | None = None


_DEFAULT_OPTS: dict | None = None


def parse_passthrough(args: list[str]) -> dict:
    """Turn raw yt-dlp CLI args into a params dict using yt-dlp's own parser.

    Delegating to the real parser means every yt-dlp flag works for free and
    stays correct across upgrades, instead of us maintaining a mapping.

    Only keys the user actually changed are returned. parse_options() yields a
    fully-populated dict, so merging it wholesale would silently overwrite our
    own settings with yt-dlp's defaults.
    """
    global _DEFAULT_OPTS
    if not args:
        return {}
    if _DEFAULT_OPTS is None:
        _DEFAULT_OPTS = dict(yt_dlp.parse_options([]).ydl_opts)
    given = dict(yt_dlp.parse_options(list(args)).ydl_opts)
    return {k: v for k, v in given.items() if k not in _DEFAULT_OPTS or _DEFAULT_OPTS[k] != v}


def build_params(cfg: Config, hooks: list | None = None) -> dict:
    """The yt-dlp parameter dict.

    Deliberately does NOT set `http_headers`. PatreonIE sets its own UA per call
    and its own lowercase `referer`, which is load-bearing for mux.com HLS
    fragments; VimeoIE copies http_headers wholesale, so anything global here
    would leak the Patreon app UA to player.vimeo.com and YouTube.
    """
    params: dict = {
        "paths": {"home": str(cfg.archive_root), "temp": str(cfg.temp_dir)},
        "outtmpl": {"default": cfg.outtmpl},
        "trim_file_name": 180,
        "windowsfilenames": False,
        "writeinfojson": cfg.write_infojson,
        "writethumbnail": cfg.write_thumbnails,
        "writesubtitles": cfg.write_subs,
        "subtitleslangs": ["en", "all"],
        "format": cfg.fmt,
        "merge_output_format": "mp4",
        "ffmpeg_location": cfg.ffmpeg_location,
        "ignoreerrors": False,  # we classify per post ourselves
        "lazy_playlist": True,
        "noplaylist": False,
        "extractor_retries": cfg.extractor_retries,
        "retries": cfg.http_retries,
        "fragment_retries": cfg.http_retries,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": cfg.frag_concurrency,
        # With >1 worker, yt-dlp's own single-line progress from several
        # downloads overwrites itself into noise, so we render our own rows.
        "quiet": cfg.quiet or cfg.agent or cfg.concurrency > 1,
        "no_warnings": False,
        "noprogress": cfg.quiet or cfg.agent or cfg.concurrency > 1,
    }
    if cfg.request_sleep:
        params["sleep_interval_requests"] = cfg.request_sleep
    if cfg.browser_spec:
        params["cookiesfrombrowser"] = cfg.browser_spec
    if cfg.cookie_file:
        params["cookiefile"] = cfg.cookie_file
    if cfg.embed_metadata:
        # The filename carries no date, so the upload date has to survive in the
        # container's own tags. Needs ffmpeg, which preflight already checks.
        params["postprocessors"] = [
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True,
             "add_infojson": "if_exists"},
        ]
    if hooks:
        params["progress_hooks"] = hooks
    params.update(cfg.extra_ytdlp)
    return params


def _final_path(info: dict) -> Path | None:
    reqs = info.get("requested_downloads") or []
    for r in reqs:
        p = r.get("filepath") or r.get("_filename")
        if p:
            return Path(p)
    return None


def classify_error(exc: Exception) -> Result:
    msg = str(exc)
    if _NO_ACCESS.search(msg):
        return Result(Outcome.NO_ACCESS, detail=msg)
    if _NO_MEDIA.search(msg):
        # Not concluded empty: yt-dlp's embed preflight runs fatal=False /
        # errnote=False, so a rate-limited preflight is indistinguishable from a
        # genuinely empty post. Retried on the next run.
        return Result(Outcome.INDETERMINATE, detail=msg)
    return Result(Outcome.FAILED, detail=msg)


def download(url: str, cfg: Config, *, simulate: bool = False, hooks: list | None = None) -> Result:
    params = build_params(cfg, hooks)
    if simulate:
        params["simulate"] = True
        params["skip_download"] = True
        params["writeinfojson"] = False
        params["writethumbnail"] = False
        params["writesubtitles"] = False
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=not simulate)
    except (DownloadError, ExtractorError) as exc:
        return classify_error(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort classification
        return Result(Outcome.FAILED, detail=f"{type(exc).__name__}: {exc}")

    if info is None:
        return Result(Outcome.INDETERMINATE, detail="extractor returned no info")

    base = dict(info)
    if base.get("_type") == "playlist":
        entries = [e for e in (base.get("entries") or []) if e]
        if not entries:
            return Result(Outcome.INDETERMINATE, detail="playlist with no entries")
        base = entries[0]

    path = None if simulate else _final_path(base)
    meta = Result(
        Outcome.COMPLETE if simulate else Outcome.FAILED,
        filepath=path,
        title=base.get("title"),
        post_id=str(base.get("id") or ""),
        uploader=base.get("channel") or base.get("uploader"),
    )
    if simulate:
        meta.detail = "simulated"
        return meta

    # Completion rule: a file on disk is never proof. Require a real, non-manifest
    # output -- a leftover .m3u8 is not a finished download.
    if path is None or not path.exists():
        meta.outcome = Outcome.INDETERMINATE
        meta.detail = "extraction reported success but no output file was produced"
        return meta
    if path.suffix.lower() == ".m3u8":
        meta.outcome = Outcome.FAILED
        meta.detail = f"output is a bare manifest, not muxed media: {path.name}"
        return meta
    if path.stat().st_size == 0:
        meta.outcome = Outcome.FAILED
        meta.detail = "output file is empty"
        return meta

    meta.outcome = Outcome.COMPLETE
    return meta
