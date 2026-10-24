from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import dataclasses
import datetime
import os
import queue
import re
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import yt_dlp
from yt_dlp.utils import sanitize_filename

from . import __version__
from .api.client import PatreonApi, post_url
from .archive import FORMATS as ARCHIVE_FORMATS, make_archive
from .auth import preflight
from .auth.cookies import materialized_jar
from .config import CREATOR_DIR, Config, DEFAULT_OUTTMPL, OUTTMPL_WITH_ID, parse_browser_spec
from .errors import Outcome, PatreonArchiveError
from .events import Emitter, ev, set_emitter
from .media import ytdlp_runner
from .media.progress import MultilineProgress
from .selection import parse_items
from .state.store import Store

# Both /collection/<id> and the newer /c/<creator>/collection/<id>.
_COLLECTION_URL = re.compile(r"patreon\.com/(?:c/[^/?#]+/)?collection/(\d+)", re.I)
# Both canonical forms: /posts/<id> and /<creator>/posts/<slug>-<id>
_POST_ID = re.compile(r"/posts/(?:.*?-)?(\d+)/?(?:[?#]|$)", re.I)


def extract_post_id(url: str) -> str | None:
    m = _POST_ID.search(url)
    return m.group(1) if m else None


def _looks_like_url(arg: str) -> str | bool:
    return arg.startswith(("http://", "https://", "www.patreon.com", "patreon.com"))

DEFAULT_OUTPUT_DIR = "downloads"


def _resolve_output(value: str | None) -> Path:
    """A bare name is a folder under the default root; anything path-shaped is a path.

    Keeps `-o films` ergonomic without making it ambiguous with `-o ~/films`.
    """
    base = Path(os.environ.get("PATREON_ARCHIVE_OUTPUT") or DEFAULT_OUTPUT_DIR)
    base = base.expanduser().resolve()
    if not value:
        return base
    # Tested on the raw string, not on Path.parts: Path('./films').parts drops the
    # './', which would read an explicitly cwd-relative path as a bare name.
    bare = "/" not in value and os.sep not in value and not value.startswith("~")
    return (base / value) if bare else Path(value).expanduser().resolve()


def _folder_name(label: str) -> str:
    """Collection title -> one safe path segment."""
    name = sanitize_filename(label, restricted=False).strip(" .")
    return name[:120] or "collection"


def _in_collection(cfg: Config, a: argparse.Namespace, label: str) -> Config:
    """Send downloads flat into <dest>/<collection>/.

    A collection is one creator's, so the creator folder would only nest every
    file a level deeper. State and temp stay at the root: the db is what makes
    re-runs resume, and it is per-destination, not per-folder.
    """
    tmpl = cfg.outtmpl if a.outtmpl else cfg.outtmpl.removeprefix(CREATOR_DIR)
    return dataclasses.replace(cfg, archive_root=cfg.archive_root / _folder_name(label),
                               outtmpl=tmpl)


@contextlib.contextmanager
def _api(cfg: Config):
    params = {"quiet": True, "no_warnings": True}
    if cfg.browser_spec:
        params["cookiesfrombrowser"] = cfg.browser_spec
    if cfg.cookie_file:
        params["cookiefile"] = cfg.cookie_file
    with yt_dlp.YoutubeDL(params) as ydl:
        yield PatreonApi(ydl, request_sleep=cfg.request_sleep)


def _post_folder(cfg: Config, a: argparse.Namespace, post_id: str) -> Config:
    """A single post that belongs to a collection lands where the collection would put it."""
    try:
        with _api(cfg) as api:
            colls = api.post_collections(post_id)
    except (PatreonArchiveError, ValueError) as exc:
        _out(f"collection lookup failed, saving under the creator folder: {exc}", cfg, err=True)
        return cfg
    if not colls:
        return cfg
    cid, title = colls[0]
    label = title or f"collection {cid}"
    also = f"  (also in {len(colls) - 1} more)" if len(colls) > 1 else ""
    cfg = _in_collection(cfg, a, label)
    _out(f"in collection {label}{also}\n  -> {cfg.archive_root}", cfg)
    return cfg


def _available(entry) -> bool:
    """Whether the session can see this entry, by the same rule -l prints.

    Patreon omits posts the session cannot view from `included`, so a row with no
    metadata at all is an entitlement boundary rather than a gap. `can_view` of
    None is unknown, which stays selectable.
    """
    if entry is None:
        return False
    if entry.can_view is False:
        return False
    return not (entry.title is None and entry.published is None)


def _pretty_date(iso: str | None) -> str:
    """'2024-06-06' -> 'June 6, 2024'. Built by hand: %-d is not portable to bionic."""
    if not iso:
        return "?"
    try:
        d = datetime.date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.strftime('%B')} {d.day}, {d.year}"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_ACCESS = 2
EXIT_INDETERMINATE = 3
EXIT_PREFLIGHT = 4
EXIT_INTERRUPTED = 130  # 128 + SIGINT, as a shell reports Ctrl-C

_EXIT = {
    Outcome.COMPLETE: EXIT_OK,
    Outcome.FAILED: EXIT_FAILED,
    Outcome.NO_ACCESS: EXIT_NO_ACCESS,
    Outcome.INDETERMINATE: EXIT_INDETERMINATE,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="patreon-archive",
        description="Download video from Patreon posts you are entitled to view.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.epilog = (
        "Any arguments after a bare '--' are passed straight to yt-dlp, e.g.\n"
        "  patreon-archive get URL -- -f 'best[height<=720]' --write-comments\n"
        "Only flags you actually pass are applied; yt-dlp defaults do not override ours."
    )
    p.formatter_class = argparse.RawDescriptionHelpFormatter

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-b", "--browser", default="firefox",
                        help="cookie source, e.g. firefox, 'chrome:Profile 1' (default: firefox)")
    common.add_argument("--cookies-file",
                        help="Netscape cookies.txt instead of a browser "
                             "(env PATREON_ARCHIVE_COOKIES)")
    common.add_argument("-o", "--output-dir", default=None,
                        help="destination. A bare name is a folder under the default "
                             f"root ('-o films' -> {DEFAULT_OUTPUT_DIR}/films); anything "
                             "path-shaped ('~/movies', './x', '/a/b') is used as given. "
                             f"Created if missing. Default root: {DEFAULT_OUTPUT_DIR} "
                             "(env PATREON_ARCHIVE_OUTPUT)")
    common.add_argument("--temp-dir", type=Path, default=None,
                        help="staging area; files are staged in a .patreon-archive-tmp "
                             "folder inside it (default: <output-dir>/.tmp)")
    common.add_argument("--state-db", type=Path, default=None,
                        help="default: <output-dir>/.patreon-archive.db")
    common.add_argument("--outtmpl", default=None,
                        help="yt-dlp output template (overrides --keep-id)")
    common.add_argument("--keep-id", action="store_true",
                        help="append [post_id] to filenames; recommended for bulk runs, "
                             "since two posts can share a title")
    common.add_argument("-f", "--format", dest="fmt", default="bestvideo*+bestaudio/best")
    common.add_argument("--ffmpeg-location", default=None)
    common.add_argument("--concurrent-fragments", type=int, default=4,
                        help="native HLS only; silently ignored when yt-dlp delegates to ffmpeg")
    common.add_argument("--request-sleep", type=float, default=0.0)
    common.add_argument("--thumbnails", action="store_true",
                        help="also save the cover image")
    common.add_argument("--infojson", action="store_true",
                        help="also save the .info.json metadata sidecar")
    common.add_argument("--subs", action="store_true",
                        help="also save subtitles, when the post has them")
    common.add_argument("--no-embed-metadata", action="store_true",
                        help="skip writing title/date/description into the file's own tags")
    common.add_argument("-q", "--quiet", action="store_true")
    common.add_argument("--agent", action="store_true",
                        help="machine-readable JSONL events on stdout (one object per "
                             "line, flushed immediately) for running as a background "
                             "task. Human text goes to stderr; stdout stays a clean "
                             "data channel. Implies --quiet for yt-dlp's own output")

    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", parents=[common],
                       help="download a post URL, or every post in a collection URL")
    g.add_argument("url")
    g.add_argument("--force", action="store_true", help="re-download even if state says complete")
    g.add_argument("-n", "--dry-run", action="store_true", help="resolve only, download nothing")
    g.add_argument("--limit", type=int, default=None, help="stop after N posts (collections)")
    g.add_argument("-i", "-I", "--items", default=None, metavar="SPEC",
                   help="which collection entries to fetch, 1-based, 1 = oldest: "
                        "'1-5', '3,7,9', '2-', '-3' (third from last), '1-3,-2-'")
    g.add_argument("-x", "--exclude", default=None, metavar="SPEC",
                   help="entries to leave out, same syntax as -i. Omit -i to mean "
                        "'everything but these': -x 2-3 downloads all but 2 and 3")
    g.add_argument("-j", "--concurrency", type=int, default=1, metavar="N",
                   help="download N posts at once (collections); each gets its own "
                        "progress row. Default 1 = yt-dlp's normal single-line progress")
    g.add_argument("-l", "--list", action="store_true",
                   help="list collection entries with their numbers, download nothing")
    g.add_argument("-a", "--archive", default=None, metavar="FORMAT",
                   choices=ARCHIVE_FORMATS,
                   help="after downloading, pack this run's files into one archive: "
                        f"{', '.join(ARCHIVE_FORMATS)}. zip/tar store without "
                        "compressing, since video does not shrink")
    g.add_argument("-y", "--yes", action="store_true",
                   help="skip the confirmation before a full-collection download")
    g.add_argument("--continue-on-error", action="store_true", default=True,
                   help="keep going when one post fails (default)")
    g.add_argument("--stop-on-error", dest="continue_on_error", action="store_false")

    sub.add_parser("auth", parents=[common], help="check cookies and environment")

    s = sub.add_parser("status", parents=[common], help="show archive state")
    s.add_argument("--post", help="show detail for one post id")

    c = sub.add_parser("cookies", parents=[common],
                       help="export browser cookies to a portable cookies.txt")
    c.add_argument("out", type=Path, nargs="?", default=Path("patreon-cookies.txt"),
                   help="destination file (default: patreon-cookies.txt)")

    sub.add_parser("help", help="print full help for every command")
    return p


def cmd_cookies(cfg: Config, out: Path) -> int:
    """Write a Netscape cookies.txt for use on a machine with no browser profile.

    This is how the tool runs on Android/Termux: browser cookie DBs there live in
    app-private storage and cannot be read without root, so the session has to be
    exported from a desktop and carried over.
    """
    pf = preflight.check(cfg)
    for w in pf.warnings:
        print(f"warning: {w}", file=sys.stderr)

    with materialized_jar(pf.browser_spec, cfg.cookie_file) as jar_file:
        if not jar_file:
            raise PatreonArchiveError("nothing to export")
        data = Path(jar_file).read_bytes()

    out = out.expanduser()
    out.write_bytes(data)
    out.chmod(0o600)

    kept = sum(1 for line in data.decode("utf-8", "replace").splitlines()
               if line.strip() and not line.startswith("#"))
    print(f"wrote {out}  ({kept} cookies, mode 0600)")
    print("\nThis file is a live credential. Treat it like a password:")
    print("  - transfer it privately (scp/USB), not via cloud sync or chat")
    print("  - it stops working when the Patreon session expires; re-export then")
    print(f"\nOn the tablet:\n  patreon-archive get --cookies-file {out.name} -o ~/storage/movies '<url>'")
    return EXIT_OK


def cmd_help() -> int:
    """One page covering every command, rather than --help per subcommand."""
    p = build_parser()
    p.print_help()
    subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    for action in subs:
        for name, parser in action.choices.items():
            if name == "help":
                continue
            print("\n" + "=" * 72)
            print(f"COMMAND: {name}")
            print("=" * 72)
            parser.print_help()
    print("\n" + "=" * 72)
    print("AGENT MODE (--agent)")
    print("=" * 72)
    print("""
Emits one JSON object per line on stdout, flushed immediately. Human text is
diverted to stderr so stdout stays a clean data channel. Run it backgrounded and
tail stdout.

  run_start            kind, collection_id, title, total, selected, concurrency,
                       post_ids
  collection_listed    entries[] of {index, post_id, url}   (with --list)
  post_start           index, post_id, url
  post_skipped         index, post_id, reason   (already complete)
  post_would_download  index, post_id, title   (with --dry-run)
  progress             index, post_id, percent, downloaded_bytes, total_bytes,
                       speed_bps, eta_s, fragment, fragment_count
                       (throttled ~2s)
  post_downloaded      index, post_id, note=muxing
  post_done            index, post_id, outcome, title, path, bytes, detail
  run_done             summary{outcome: count}, selected, exit_code

index is the entry's number as --list prints it (1 = oldest), present for
collection entries. For a single post, progress, post_downloaded and
post_error carry index 0, and the other post_* events have none. A dry run
emits post_done for a post it cannot resolve, as a real run would, and
post_would_download otherwise.

Exit codes: 0 complete, 1 failed, 2 no_access, 3 indeterminate, 4 preflight error, 130 interrupted.

Example:
  patreon-archive get URL -I 1-5 -j 3 --agent > events.jsonl 2> run.log &
""")
    return EXIT_OK


def _config(a: argparse.Namespace) -> Config:
    # Env defaults let a machine with no browser (Android) carry its cookie file
    # and destination without a shell wrapper. Explicit flags still win.
    if not a.cookies_file:
        a.cookies_file = os.environ.get("PATREON_ARCHIVE_COOKIES") or None

    out = _resolve_output(a.output_dir)
    # Staging gets a folder of its own. Before a forced download yt-dlp clears
    # a video's staging path, which in a folder shared with finished files can
    # be another video's. <dest>/.tmp is ours already.
    temp = a.temp_dir / ".patreon-archive-tmp" if a.temp_dir else out / ".tmp"
    return Config(
        archive_root=out,
        temp_dir=temp.expanduser().resolve(),
        state_db=(a.state_db or out / ".patreon-archive.db").expanduser().resolve(),
        browser_spec=None if a.cookies_file else parse_browser_spec(a.browser),
        cookie_file=a.cookies_file,
        outtmpl=a.outtmpl or (OUTTMPL_WITH_ID if a.keep_id else DEFAULT_OUTTMPL),
        fmt=a.fmt,
        ffmpeg_location=a.ffmpeg_location,
        frag_concurrency=a.concurrent_fragments,
        request_sleep=a.request_sleep,
        write_thumbnails=a.thumbnails,
        write_infojson=a.infojson,
        write_subs=a.subs,
        embed_metadata=not a.no_embed_metadata,
        quiet=a.quiet,
        force=getattr(a, "force", False),
        concurrency=max(1, getattr(a, "concurrency", 1)),
        agent=getattr(a, "agent", False),
        extra_ytdlp=ytdlp_runner.parse_passthrough(getattr(a, "ytdlp_args", []) or []),
    )


def cmd_auth(cfg: Config) -> int:
    pf = preflight.check(cfg)
    print(f"ffmpeg          {pf.ffmpeg}")
    print(f"yt-dlp          {ytdlp_runner.YTDLP_VERSION}")
    print(f"cookies loaded  {pf.cookie_count}")
    print(f"session_id      {'present' if pf.has_session else 'MISSING'}")
    print(f"device_id       {'present' if pf.has_device_id else 'absent'}")
    for w in pf.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


_MEDIA_STATE = {
    Outcome.COMPLETE: "complete",
    Outcome.NO_ACCESS: "skipped_no_access",
    Outcome.INDETERMINATE: "indeterminate",
    Outcome.FAILED: "failed",
}


class _Claims:
    """Who holds each title-only name, shared by every worker in one run.

    `lock` is the lock Store.__init__ relies on: it serialises every store access
    under -j, including the reads yt-dlp makes through this registry.
    """

    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()
        self._names: dict[str, str] = {}

    def __call__(self, video_id: str, path: str) -> bool:
        # Keyed as Store.path_key keys ownership: under -j two case aliases are
        # claimed before either exists on disk. On a case-sensitive filesystem
        # that costs at most a needless suffix.
        key = Store.path_key(path)
        with self.lock:
            holder = self._names.get(key)
            if holder is not None:
                return holder == video_id
            owner = self.store.output_owner(path)
            if owner != video_id and (owner is not None or Path(path).exists()):
                return False
            self._names[key] = video_id
            return True

    def owner(self, path: str) -> str | None:
        """The video recorded as having completed `path`, read under the lock."""
        with self.lock:
            return self.store.output_owner(path)


def _persist(store: Store, res, pid: str, url: str) -> None:
    """Record the post under the requested id `pid`, and one media row per video."""
    store.upsert_post(
        post_id=pid,
        outcome=res.outcome,
        tool_version=__version__,
        ytdlp_version=ytdlp_runner.YTDLP_VERSION,
        title=res.title,
        post_url=url,
        can_view=0 if res.outcome == Outcome.NO_ACCESS else 1,
        media_expected=len(res.videos),
        media_obtained=sum(v.outcome == Outcome.COMPLETE for v in res.videos),
        outcome_detail=res.detail,
    )
    if not res.videos:
        store.record_media(
            post_id=pid,
            source_kind="ytdlp_post_url",
            state=_MEDIA_STATE[res.outcome],
            detail=res.detail,
        )
        return
    keys = set()
    for v in res.videos:
        output = str(v.filepath) if v.filepath else None
        store.record_media(
            post_id=pid,
            source_kind="ytdlp_post_url",
            state=_MEDIA_STATE[v.outcome],
            media_id=v.video_id,
            output_path=output,
            detail=v.detail,
        )
        keys.add(Store.dedup_key(v.video_id, None, output))
    if res.outcome == Outcome.COMPLETE:
        # Drop rows this result lacks, such as a pre-0.4 row keyed by its path.
        store.prune_media(pid, keys)


def _one_post(cfg: Config, store: Store, url: str, dry_run: bool,
              collect: list[Path] | None = None, claims: _Claims | None = None, *,
              index: int | None = None, hooks: list | None = None,
              log: Callable[..., None] | None = None) -> Outcome:
    """Download a single post URL and record its outcome. Never raises.

    `collect` gathers the files this run actually produced, for -a.
    """
    claims = claims or _Claims(store)
    # Only -j passes `log`. Its lines interleave across posts, so each names its
    # entry; the serial loop heads each post with its number instead.
    at = f"[{index}] " if index is not None and log is not None else ""
    if log is None:
        def log(msg: str, err: bool = False) -> None:
            _out(msg, cfg, err=err)
    numbered = {"index": index} if index is not None else {}

    post_id = extract_post_id(url)
    if post_id:
        with claims.lock:
            proceed, why = store.should_process(post_id, force=cfg.force)
        if not proceed:
            ev().emit("post_skipped", **numbered, post_id=post_id, reason=why)
            log(f"skip {at}{post_id}: {why}")
            return Outcome.COMPLETE

    # Hand yt-dlp the canonical URL. Share links and tracking params ride along
    # otherwise, and extractor URL matching is sensitive to them.
    target = post_url(post_id) if post_id else url

    ev().emit("post_start", **numbered, post_id=post_id, url=target)
    hooks = list(hooks or [])
    if cfg.agent:
        hooks.append(ev().hook(index or 0, post_id or url))
    # Not under the lock: yt-dlp calls the claim on this thread, and the claim
    # takes the lock, which is not reentrant.
    res = ytdlp_runner.download(target, cfg, simulate=dry_run, hooks=hooks, claim=claims,
                                owner=claims.owner)
    pid = post_id or res.post_id or url

    if dry_run and res.outcome == Outcome.COMPLETE:
        ev().emit("post_would_download", **numbered, post_id=pid, title=res.title)
        log(f"would download: {res.title or '?'}  {at}[{pid}]")
        return res.outcome

    if not dry_run:
        with claims.lock:
            _persist(store, res, pid, url)

    ev().emit(
        "post_done", **numbered, post_id=pid, outcome=str(res.outcome), title=res.title,
        path=str(res.filepath) if res.filepath else None,
        bytes=res.filepath.stat().st_size if (res.filepath and res.filepath.exists()) else None,
        detail=res.detail,
    )
    if res.outcome == Outcome.COMPLETE:
        for v in res.videos:
            if collect is not None:
                with claims.lock:
                    collect.append(v.filepath)
            mb = v.filepath.stat().st_size / (1024 * 1024)
            log(f"  ok  {at}{v.filepath.name}  ({mb:.1f} MiB)")
    elif res.outcome == Outcome.NO_ACCESS:
        log(f"  no access  {at}[{pid}] {res.detail}", err=True)
    elif res.outcome == Outcome.INDETERMINATE:
        log(f"  indeterminate  {at}[{pid}] {res.detail} (will retry on re-run)", err=True)
    else:
        log(f"  failed  {at}[{pid}] {res.detail}", err=True)
    return res.outcome


def cmd_get(cfg: Config, a: argparse.Namespace) -> int:
    pf = preflight.check(cfg)
    for w in pf.warnings:
        print(f"warning: {w}", file=sys.stderr)
    # preflight may have switched to a profile that actually has a session
    cfg = dataclasses.replace(cfg, browser_spec=pf.browser_spec)

    cfg.archive_root.mkdir(parents=True, exist_ok=True)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)

    m = _COLLECTION_URL.search(a.url)

    # Extract browser cookies once, not once per post.
    with materialized_jar(cfg.browser_spec, cfg.cookie_file) as jar_file:
        if jar_file and jar_file != cfg.cookie_file:
            cfg = dataclasses.replace(cfg, browser_spec=None, cookie_file=jar_file)

        if m:
            return _collection(cfg, a, m.group(1))

        post_id = extract_post_id(a.url)
        if post_id:
            cfg = _post_folder(cfg, a, post_id)

        store = Store(cfg.state_db)
        produced: list[Path] = []
        try:
            outcome = _one_post(cfg, store, a.url, a.dry_run, produced)
        finally:
            store.close()
        _maybe_archive(cfg, a, produced)
        return _EXIT[outcome]


@contextlib.contextmanager
def _sigint_watch():
    """Yield an Event that any Ctrl-C during the block sets.

    yt-dlp keeps a live recording that Ctrl-C stops and returns as if it had
    finished (downloader/external.py:50), so the KeyboardInterrupt never reaches
    us; the flag still does. The previous handler runs after it, so Ctrl-C
    raises as before everywhere else. Only the main thread can set a handler,
    and an ignored SIGINT, as in a job a script puts in the background, stays
    ignored.
    """
    seen = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    if threading.current_thread() is not threading.main_thread() or not callable(previous):
        yield seen
        return

    def handler(signum, frame):
        seen.set()
        previous(signum, frame)

    try:
        # Installed inside the try, so a Ctrl-C right after still restores it.
        signal.signal(signal.SIGINT, handler)
        yield seen
    finally:
        signal.signal(signal.SIGINT, previous)


def _collection(cfg: Config, a: argparse.Namespace, collection_id: str) -> int:
    """Collections are unsupported by yt-dlp, so we enumerate them ourselves."""
    with _api(cfg) as api:
        coll = api.collection(collection_id)

    label = coll.title or f"collection {collection_id}"
    total = len(coll.post_ids)

    entries = coll.entries or []
    by_id = {e.post_id: e for e in entries}

    if a.list:
        # post_id and url stay in the event: the human table drops them, but
        # machine consumers still need an identifier.
        ev().emit("collection_listed", collection_id=collection_id, title=label,
                  total=total,
                  entries=[{"index": n, "post_id": p, "url": post_url(p),
                            "title": (by_id.get(p).title if by_id.get(p) else None),
                            "published": (by_id.get(p).published if by_id.get(p) else None),
                            "duration": (by_id.get(p).duration if by_id.get(p) else None)}
                           for n, p in enumerate(coll.post_ids, 1)])
        _out(f"{label}: {total} posts  (1 = oldest)", cfg)
        for n, pid in enumerate(coll.post_ids, 1):
            e = by_id.get(pid)
            title = (e.title if e and e.title else "?")
            mark = "" if _available(e) else "  [no access]"
            _out(f"  {n:3}  {_hms(e.duration if e else None):>9}  "
                 f"{_pretty_date(e.published if e else None):18}  {title}{mark}", cfg)
        return EXIT_OK

    picked = (parse_items(a.items or "", total, a.exclude)
              if (a.items or a.exclude) else list(range(1, total + 1)))

    # Indices number the whole collection, exactly as -l prints them, so -3 means
    # the same entry in both. Entries the session cannot view are then dropped, so
    # a tail grab yields downloadable posts instead of a run of no_access rows.
    # Filtered before --limit, so --limit 5 means five real downloads.
    blocked = [n for n in picked if not _available(by_id.get(coll.post_ids[n - 1]))]
    picked = [n for n in picked if n not in set(blocked)]
    if a.limit:
        picked = picked[: a.limit]

    if blocked:
        _out(f"skipping {len(blocked)} of {len(blocked) + len(picked)} selected: "
             f"no access to {', '.join(str(n) for n in blocked)}", cfg)
    if not picked:
        ev().emit("run_done", summary={}, selected=0, exit_code=EXIT_NO_ACCESS)
        _out("nothing to download: no access to any selected entry", cfg, err=True)
        return EXIT_NO_ACCESS

    chosen = [(n, coll.post_ids[n - 1]) for n in picked]
    # Progress rows and per-post lines read better with a title than a post id.
    labels = {pid: (by_id[pid].title if by_id.get(pid) and by_id[pid].title else None)
              for _, pid in chosen}

    # Everything lands in a folder named after the collection, so two collections
    # sharing a destination stay separate.
    cfg = _in_collection(cfg, a, label)
    cfg.archive_root.mkdir(parents=True, exist_ok=True)

    # A full-collection download is the classic fat-finger outcome (an unquoted
    # share URL eats the flags after its '&'), so it asks first. Anything that
    # shows intent skips the prompt: -I/--limit/-n/-y, agent mode, or no TTY.
    if (not a.items and not a.limit and not a.dry_run and not a.yes
            and not cfg.agent and sys.stdin.isatty()):
        _out(f"{label}: about to download ALL {len(picked)} accessible entries "
             f"(often 1.5-2 GB each). -I picks a subset; -y skips this prompt.", cfg)
        try:
            reply = input("proceed? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            _out("aborted", cfg)
            return EXIT_OK

    ev().emit("run_start", kind="collection", collection_id=collection_id,
              title=label, total=total, selected=len(chosen),
              concurrency=cfg.concurrency, dry_run=a.dry_run,
              post_ids=[pid for _, pid in chosen])

    tally: dict[Outcome, int] = {}
    produced: list[Path] = []
    store = Store(cfg.state_db)
    claims = _Claims(store)
    lock = threading.Lock()
    interrupted = False

    def record(outcome: Outcome) -> None:
        with lock:
            tally[outcome] = tally.get(outcome, 0) + 1

    try:
        with _sigint_watch() as sigint:
            _out(f"{label}: {total} posts, selected {len(chosen)}"
                 f"{f', {cfg.concurrency} at a time' if cfg.concurrency > 1 else ''}\n"
                 f"  -> {cfg.archive_root}\n", cfg)
            if cfg.concurrency > 1:
                interrupted = _parallel(cfg, store, chosen, a, record, produced,
                                        labels, claims, sigint)
            else:
                for n, pid in chosen:
                    if sigint.is_set():
                        break
                    _out(f"[{n}/{total}] {labels.get(pid) or f'post {pid}'}", cfg)
                    try:
                        outcome = _one_post(cfg, store, post_url(pid), a.dry_run,
                                            produced, claims, index=n)
                    except Exception as exc:  # noqa: BLE001
                        outcome = Outcome.FAILED
                        print(f"  failed  [{pid}] {type(exc).__name__}: {exc}",
                              file=sys.stderr)
                    record(outcome)
                    if outcome != Outcome.COMPLETE and not a.continue_on_error:
                        break
            interrupted = interrupted or sigint.is_set()
    except KeyboardInterrupt:
        # Outside the watch, so a Ctrl-C as its handler goes in or comes back out
        # still ends the run as interrupted, not through main().
        interrupted = True
    finally:
        store.close()
    if interrupted:
        print("\ninterrupted", file=sys.stderr)

    if interrupted:
        code = EXIT_INTERRUPTED
    elif tally.get(Outcome.FAILED):
        code = EXIT_FAILED
    elif tally.get(Outcome.INDETERMINATE):
        code = EXIT_INDETERMINATE
    elif tally.get(Outcome.NO_ACCESS) and not tally.get(Outcome.COMPLETE):
        code = EXIT_NO_ACCESS
    else:
        code = EXIT_OK

    ev().emit("run_done", summary={str(k): v for k, v in tally.items()},
              selected=len(chosen), exit_code=code)
    _out("\nsummary", cfg)
    for oc in (Outcome.COMPLETE, Outcome.NO_ACCESS, Outcome.INDETERMINATE, Outcome.FAILED):
        if tally.get(oc):
            _out(f"  {oc:16} {tally[oc]}", cfg)

    if not interrupted:
        _maybe_archive(cfg, a, produced)
    return code


def _maybe_archive(cfg: Config, a: argparse.Namespace, produced: list[Path]) -> None:
    """Pack what this run downloaded. Never turns a good run into a failure."""
    if not getattr(a, "archive", None) or a.dry_run:
        return
    if not produced:
        _out("nothing new downloaded, no archive written", cfg)
        return
    try:
        out = make_archive(produced, cfg.archive_root, a.archive)
    except PatreonArchiveError as exc:
        _out(f"archive failed: {exc}", cfg, err=True)
        return
    mb = out.stat().st_size / (1024 * 1024)
    ev().emit("archived", path=str(out), files=len(produced), bytes=out.stat().st_size)
    _out(f"archived {len(produced)} file(s) -> {out.name}  ({mb:.1f} MiB)", cfg)



def _parallel(cfg: Config, store: Store, chosen, a, record,
              produced: list[Path] | None = None,
              labels: dict[str, str | None] | None = None,
              claims: _Claims | None = None,
              sigint: threading.Event | None = None) -> bool:
    """Run N posts at once, each owning one live progress row."""
    slots = min(cfg.concurrency, len(chosen))
    free: queue.Queue[int] = queue.Queue()
    for i in range(slots):
        free.put(i)
    claims = claims or _Claims(store)
    sigint = sigint or threading.Event()

    with MultilineProgress(slots) as ui:
        def log(msg: str, err: bool = False) -> None:
            ui.log(msg)  # ui.log writes to stderr already, so `err` changes nothing

        def work(n: int, pid: str) -> None:
            # Each worker records its own outcome, so posts that drain after Ctrl-C count.
            slot = free.get()
            label = f"[{n}] {(labels or {}).get(pid) or pid}"
            try:
                ui.set(slot, f"  {label:38.38} starting…")
                outcome = _one_post(cfg, store, post_url(pid), a.dry_run, produced, claims,
                                    index=n, hooks=[ui.hook(slot, label)], log=log)
            except Exception as exc:  # noqa: BLE001
                outcome = Outcome.FAILED
                ui.log(f"  failed  [{n}] {type(exc).__name__}: {exc}")
            finally:
                ui.set(slot, "")
                free.put(slot)
            record(outcome)

        with cf.ThreadPoolExecutor(max_workers=slots) as pool:
            # Submitted inside the try, so a Ctrl-C mid-submit still cancels the queue.
            futures: list[cf.Future] = []
            interrupted = False
            try:
                for n, pid in chosen:
                    futures.append(pool.submit(work, n, pid))
                for fut in cf.as_completed(futures):
                    fut.result()  # re-raises a worker's KeyboardInterrupt
                    if sigint.is_set():
                        interrupted = True
                        break
            except KeyboardInterrupt:
                interrupted = True
            if interrupted:
                # Let in-flight muxes finish rather than corrupting outputs.
                ui.log("interrupted; letting in-flight downloads finish…")
                pool.shutdown(wait=True, cancel_futures=True)
    return interrupted


def cmd_status(cfg: Config, post: str | None) -> int:
    store = Store(cfg.state_db)
    try:
        if post:
            row = store.get_post(post)
            if not row:
                print(f"no record for {post}")
                return EXIT_FAILED
            for k in row.keys():
                print(f"{k:16} {row[k]}")
            print("\nmedia:")
            for m in store.media_for(post):
                print(f"  {m['source_kind']:16} {m['state']:18} {m['output_path'] or '-'}")
            return EXIT_OK
        rows = store.summary()
        if not rows:
            print("no posts recorded yet")
        for r in rows:
            print(f"{r['outcome']:16} {r['n']}")
        return EXIT_OK
    finally:
        store.close()


def _hms(seconds: float | None) -> str:
    if not seconds:
        return "-"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"


def _out(msg: str, cfg: Config, *, err: bool = False) -> None:
    """Human text. In agent mode it all goes to stderr so stdout stays JSONL."""
    print(msg, file=sys.stderr if (err or cfg.agent) else sys.stdout)


_SPEC_FLAGS = ("-i", "-I", "--items", "-x", "--exclude")
_DASH_SPEC = re.compile(r"-[\d,-]+")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Everything after a bare '--' belongs to yt-dlp, not us.
    ytdlp_args: list[str] = []
    if "--" in raw:
        i = raw.index("--")
        raw, ytdlp_args = raw[:i], raw[i + 1:]

    if raw and raw[0] == "help":
        return cmd_help()

    # A bare URL means `get`, so the common case is `patreon-archive <url>`.
    if raw and _looks_like_url(raw[0]):
        raw.insert(0, "get")

    # argparse takes a spec like '-3-' (the last three) for a flag, which leaves
    # `-i -3-` without its value, so the two are joined as `-i=-3-`.
    joined: list[str] = []
    for arg in raw:
        if joined and joined[-1] in _SPEC_FLAGS and _DASH_SPEC.fullmatch(arg):
            joined[-1] = f"{joined[-1]}={arg}"
        else:
            joined.append(arg)

    a = build_parser().parse_args(joined)
    a.ytdlp_args = ytdlp_args
    cfg = _config(a)
    set_emitter(Emitter(cfg.agent))
    try:
        if a.cmd == "auth":
            return cmd_auth(cfg)
        if a.cmd == "get":
            return cmd_get(cfg, a)
        if a.cmd == "status":
            return cmd_status(cfg, a.post)
        if a.cmd == "cookies":
            return cmd_cookies(cfg, a.out)
    except PatreonArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
