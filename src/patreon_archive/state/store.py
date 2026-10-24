from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from ..errors import Outcome

SCHEMA = Path(__file__).with_name("schema.sql")
USER_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


class Store:
    """Owns every SQL statement. Nothing else in the package touches sqlite3."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe here only because every caller in the
        # parallel path holds a lock around store access; sqlite3 connections are
        # otherwise bound to their creating thread.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.read_text())
        self.db.execute(f"PRAGMA user_version = {USER_VERSION}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def dedup_key(media_id: str | None, url_md5: str | None, output_path: str | None) -> str:
        """The pre-download identity rule, in the one module that knows it.

        SQLite cannot express this as a constraint, so it is computed here and
        stored as a column.
        """
        return media_id or url_md5 or output_path or "-"

    @staticmethod
    def path_key(path: str | Path) -> str:
        """The ownership identity of an output path.

        Unicode canonical caseless matching, NFD(casefold(NFD(path))), on every
        filesystem: APFS and Android storage treat paths that differ only in case,
        or in NFC/NFD form, as one file, and casefolding alone can reintroduce
        decomposed forms. SQLite's NOCASE and lower() fold ASCII only, so the
        comparison happens here rather than in SQL.
        """
        nfd = unicodedata.normalize("NFD", str(Path(path)))
        return unicodedata.normalize("NFD", nfd.casefold())

    def get_post(self, post_id: str) -> sqlite3.Row | None:
        cur = self.db.execute("SELECT * FROM post WHERE post_id = ?", (post_id,))
        return cur.fetchone()

    def should_process(self, post_id: str, *, force: bool) -> tuple[bool, str]:
        """Invalidation rule. Returns (process?, reason)."""
        row = self.get_post(post_id)
        if force:
            return True, "forced"
        if row is None:
            return True, "new"
        if row["outcome"] in (Outcome.INDETERMINATE, Outcome.FAILED):
            return True, f"previous outcome {row['outcome']}"
        if row["outcome"] == Outcome.NO_ACCESS:
            return True, "re-checking access"
        # complete: verify the recorded outputs still exist before trusting it
        missing = [
            r["output_path"]
            for r in self.media_for(post_id)
            if r["state"] == "complete"
            and (not r["output_path"] or not Path(r["output_path"]).exists())
        ]
        if missing:
            return True, "recorded output missing on disk"
        return False, "already complete"

    def media_for(self, post_id: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM post_media WHERE post_id = ?", (post_id,))
        )

    def upsert_post(
        self,
        *,
        post_id: str,
        outcome: Outcome,
        tool_version: str,
        ytdlp_version: str,
        title: str | None = None,
        post_url: str | None = None,
        campaign_id: str | None = None,
        post_type: str | None = None,
        embed_provider: str | None = None,
        published_at: str | None = None,
        can_view: int | None = None,
        media_expected: int = 0,
        media_obtained: int = 0,
        outcome_detail: str | None = None,
    ) -> None:
        now = _now()
        completed = now if outcome == Outcome.COMPLETE else None
        self.db.execute(
            """
            INSERT INTO post (post_id, campaign_id, post_type, embed_provider, published_at,
                              can_view, title, post_url, outcome, outcome_detail,
                              media_expected, media_obtained, tool_version, ytdlp_version,
                              first_seen_at, last_attempt_at, attempt_count, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(post_id) DO UPDATE SET
                campaign_id    = COALESCE(excluded.campaign_id, post.campaign_id),
                post_type      = COALESCE(excluded.post_type, post.post_type),
                embed_provider = COALESCE(excluded.embed_provider, post.embed_provider),
                published_at   = COALESCE(excluded.published_at, post.published_at),
                can_view       = COALESCE(excluded.can_view, post.can_view),
                title          = COALESCE(excluded.title, post.title),
                post_url       = COALESCE(excluded.post_url, post.post_url),
                outcome        = excluded.outcome,
                outcome_detail = excluded.outcome_detail,
                media_expected = excluded.media_expected,
                media_obtained = excluded.media_obtained,
                tool_version   = excluded.tool_version,
                ytdlp_version  = excluded.ytdlp_version,
                last_attempt_at= excluded.last_attempt_at,
                attempt_count  = post.attempt_count + 1,
                completed_at   = COALESCE(excluded.completed_at, post.completed_at)
            """,
            (post_id, campaign_id, post_type, embed_provider, published_at, can_view,
             title, post_url, str(outcome), outcome_detail, media_expected, media_obtained,
             tool_version, ytdlp_version, now, now, completed),
        )
        self.db.commit()

    def record_media(
        self,
        *,
        post_id: str,
        source_kind: str,
        state: str,
        media_id: str | None = None,
        url_md5: str | None = None,
        provider: str | None = None,
        output_path: str | None = None,
        detail: str | None = None,
    ) -> None:
        content_sha = None
        if state == "complete" and output_path and Path(output_path).exists():
            p = Path(output_path)
            content_sha = sha256_file(p)
            self.db.execute(
                """INSERT INTO blob (content_sha256, size_bytes, mimetype, ext, first_path, created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(content_sha256) DO NOTHING""",
                (content_sha, p.stat().st_size, None, p.suffix.lstrip("."), str(p), _now()),
            )
        self.db.execute(
            """INSERT INTO post_media (post_id, media_id, url_md5, source_kind, provider,
                                       content_sha256, output_path, state, detail, updated_at, dedup_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(post_id, source_kind, dedup_key) DO UPDATE SET
                   content_sha256 = excluded.content_sha256,
                   output_path    = excluded.output_path,
                   state          = excluded.state,
                   detail         = excluded.detail,
                   updated_at     = excluded.updated_at""",
            (post_id, media_id, url_md5, source_kind, provider, content_sha, output_path,
             state, detail, _now(), self.dedup_key(media_id, url_md5, output_path)),
        )
        self.db.commit()

    def output_owner(self, path: str) -> str | None:
        """The video recorded as having completed `path`, or a case alias of it.

        Rows written before 0.4 carry no media id; their post id is the owner.
        Should several rows claim one file, the first to record it wins: its
        download is the one whose content is on disk. Whether the file is still
        there does not matter: a recorded path stays its owner's.
        """
        key = self.path_key(path)
        cur = self.db.execute(
            """SELECT COALESCE(media_id, post_id) AS owner, output_path FROM post_media
               WHERE output_path IS NOT NULL AND state = 'complete' ORDER BY rowid"""
        )
        for row in cur:
            if self.path_key(row["output_path"]) == key:
                return row["owner"]
        return None

    def prune_media(self, post_id: str, keep_keys: set[str]) -> None:
        keep = sorted(keep_keys)
        self.db.execute(
            "DELETE FROM post_media WHERE post_id = ? "
            f"AND dedup_key NOT IN ({','.join('?' * len(keep))})",
            (post_id, *keep),
        )
        self.db.commit()

    def duplicate_of(self, content_sha256: str) -> str | None:
        cur = self.db.execute(
            "SELECT first_path FROM blob WHERE content_sha256 = ?", (content_sha256,)
        )
        row = cur.fetchone()
        return row["first_path"] if row else None

    def summary(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT outcome, COUNT(*) AS n FROM post GROUP BY outcome ORDER BY n DESC"
            )
        )
