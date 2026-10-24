from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yt_dlp
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.utils import DownloadError, ExtractorError

from ..auth.cookies import private_copy
from ..config import Config, is_builtin_outtmpl
from ..errors import Outcome

YTDLP_VERSION = yt_dlp.version.__version__

# yt-dlp distinguishes these two at patreon.py:504-512 via current_user_can_view.
# We match on its messages because, driving it by post URL, that is the signal we get.
_NO_ACCESS = re.compile(r"do not have access", re.I)
_NO_MEDIA = re.compile(r"no supported media found", re.I)


# Spelled as in config.DEFAULT_OUTTMPL, so finding it marks that template.
SUFFIX_FIELD = "%(pa_suffix|)s"


class Video(NamedTuple):
    """One file of a post.

    `video_id` is yt-dlp's entry id: `<pid>-<n>` in a multi-video post, else the
    post's id, or the provider's for an embed.
    """
    video_id: str
    outcome: Outcome
    filepath: Path | None
    detail: str | None = None


@dataclass
class Result:
    outcome: Outcome
    detail: str | None = None
    title: str | None = None
    # The extractor's top-level id, which for an embed can be the provider's;
    # never a multi-video entry id. cli stores state under the requested id.
    post_id: str | None = None
    uploader: str | None = None
    duplicate_of: Path | None = None
    videos: list[Video] = field(default_factory=list)

    @property
    def filepath(self) -> Path | None:
        """The first video's file, for reports that carry one path per post."""
        return self.videos[0].filepath if self.videos else None


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
    # Only a user template keeps the trim: ours bound each segment instead, and
    # trimming the rendered path would cut the suffix off (config.py).
    if not is_builtin_outtmpl(params["outtmpl"].get("default", "")):
        params.setdefault("trim_file_name", 180)
    return params


class _ClaimName(PostProcessor):
    """Gives a video its title-only name, or appends " [<id>]" when that is taken.

    Runs at the `video` stage, which yt-dlp calls before it computes the filename
    (YoutubeDL.process_info, YoutubeDL.py:3360 then 3365), so the field set here
    is in the name the download is written under.
    """

    def __init__(self, claim: Callable[[str, str], bool]):
        super().__init__()
        self._claim = claim

    def run(self, info):
        vid = str(info["id"])
        path = self._downloader.prepare_filename(info)
        if not path or self._claim(vid, path):
            return [], info
        info["pa_suffix"] = f" [{vid}]"
        alt = self._downloader.prepare_filename(info)
        if alt == path or not self._claim(vid, alt):
            # Never fall back onto a file that is someone else's. Not a
            # PostProcessingError: yt-dlp only logs that one here, runs the
            # download step anyway, and raises it after (never, with
            # --ignore-errors). See YoutubeDL.pre_process and run_pp,
            # YoutubeDL.py:3843 and 3816.
            raise DownloadError(f"no free name for {vid}: {alt} is taken")
        return [], info


class _Finished(PostProcessor):
    """Notes each video once it sits at its final path.

    A multi-video post can fail on a later video after earlier ones are in place.
    Those still have to be recorded: unrecorded, a re-run would read their names
    as taken by a stranger and download them again under a suffix.
    """

    def __init__(self):
        super().__init__()
        self.paths: dict[str, Path] = {}

    def run(self, info):
        if info.get("filepath"):
            self.paths.setdefault(str(info["id"]), Path(info["filepath"]))
        return [], info


def _final_path(info: dict) -> Path | None:
    reqs = info.get("requested_downloads") or []
    for r in reqs:
        p = r.get("filepath") or r.get("_filename")
        if p:
            return Path(p)
    return None


def _judge(video_id: str, path: Path | None) -> Video:
    """Completion rule: a file on disk is never proof on its own. Require a real,
    non-empty, non-manifest output -- a leftover .m3u8 is not a finished download.
    """
    if path is None or not path.exists():
        return Video(video_id, Outcome.INDETERMINATE, path,
                     "extraction reported success but no output file was produced")
    if path.suffix.lower() == ".m3u8":
        return Video(video_id, Outcome.FAILED, path,
                     f"output is a bare manifest, not muxed media: {path.name}")
    if path.stat().st_size == 0:
        return Video(video_id, Outcome.FAILED, path, "output file is empty")
    return Video(video_id, Outcome.COMPLETE, path)


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


def download(url: str, cfg: Config, *, simulate: bool = False, hooks: list | None = None,
             claim: Callable[[str, str], bool] | None = None) -> Result:
    """Download one post. `claim(video_id, path)` says whether the video may take
    the title-only `path`; when it may not, the name gets " [<id>]".

    Each call loads its own private_copy of whatever cookies file the merged
    params name, so a passthrough --cookies is never written either.
    """
    params = build_params(cfg, hooks)
    with private_copy(params.get("cookiefile")) as jar_file:
        if jar_file:
            params["cookiefile"] = jar_file
        return _download(url, params, simulate, claim)


def _download(url: str, params: dict, simulate: bool,
              claim: Callable[[str, str], bool] | None) -> Result:
    if simulate:
        params["simulate"] = True
        params["skip_download"] = True
        params["writeinfojson"] = False
        params["writethumbnail"] = False
        params["writesubtitles"] = False
    finished = _Finished()
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            ydl.add_post_processor(finished, when="after_move")
            if claim and not simulate and SUFFIX_FIELD in params["outtmpl"].get("default", ""):
                ydl.add_post_processor(_ClaimName(claim), when="video")
            info = ydl.extract_info(url, download=not simulate)
    except (DownloadError, ExtractorError) as exc:
        res = classify_error(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort classification
        res = Result(Outcome.FAILED, detail=f"{type(exc).__name__}: {exc}")
    else:
        return _result(info, simulate)
    res.videos = [_judge(vid, path) for vid, path in finished.paths.items()]
    return res


def _result(info: dict | None, simulate: bool) -> Result:
    if info is None:
        return Result(Outcome.INDETERMINATE, detail="extractor returned no info")

    multi = info.get("_type") == "playlist"
    entries = list(info.get("entries") or []) if multi else [info]
    if not entries:
        return Result(Outcome.INDETERMINATE, detail="playlist with no entries")

    pid = str(info.get("id") or "")
    first = next((e for e in entries if e), {})
    meta = Result(
        Outcome.COMPLETE,
        title=info.get("title") or first.get("title"),
        post_id=pid,
        uploader=(info.get("channel") or info.get("uploader")
                  or first.get("channel") or first.get("uploader")),
    )
    if simulate:
        meta.detail = "simulated"
        return meta

    # yt-dlp leaves None where an entry failed under --ignore-errors
    # (YoutubeDL.__process_playlist, YoutubeDL.py:2181); that video is missing,
    # and the post is not complete without it.
    meta.videos = [
        _judge(str(e.get("id") or ""), _final_path(e)) if e
        else Video(f"{pid}-{n}", Outcome.FAILED, None, "video missing from the extraction result")
        for n, e in enumerate(entries, 1)
    ]
    bad = [v for v in meta.videos if v.outcome != Outcome.COMPLETE]
    if bad:
        worst = next((v for v in bad if v.outcome == Outcome.FAILED), bad[0])
        meta.outcome = worst.outcome
        meta.detail = f"{worst.video_id}: {worst.detail}" if multi else worst.detail
    return meta
