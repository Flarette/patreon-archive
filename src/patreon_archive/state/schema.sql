PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaign (
    campaign_id   TEXT PRIMARY KEY,
    vanity        TEXT,
    name          TEXT,
    first_seen_at TEXT NOT NULL,
    last_sync_at  TEXT
);

-- Resume: one live cursor per (campaign, sort-order) enumeration.
CREATE TABLE IF NOT EXISTS enumeration_cursor (
    campaign_id     TEXT NOT NULL REFERENCES campaign(campaign_id),
    sort_key        TEXT NOT NULL DEFAULT '-published_at',
    page_cursor     TEXT,
    next_link       TEXT,
    pages_done      INTEGER NOT NULL DEFAULT 0,
    oldest_seen_at  TEXT,
    newest_seen_at  TEXT,
    completed       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (campaign_id, sort_key)
);

CREATE TABLE IF NOT EXISTS post (
    post_id        TEXT PRIMARY KEY,
    campaign_id    TEXT,
    post_type      TEXT,
    embed_provider TEXT,
    published_at   TEXT,
    edited_at      TEXT,
    comment_count  INTEGER,
    can_view       INTEGER,
    title          TEXT,
    post_url       TEXT,
    outcome        TEXT NOT NULL CHECK (outcome IN
                     ('complete','no_access','indeterminate','failed')),
    outcome_detail TEXT,
    media_expected INTEGER NOT NULL DEFAULT 0,
    media_obtained INTEGER NOT NULL DEFAULT 0,
    tool_version   TEXT NOT NULL,
    ytdlp_version  TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS post_by_campaign_date ON post(campaign_id, published_at DESC);
CREATE INDEX IF NOT EXISTS post_by_outcome       ON post(outcome, campaign_id);

-- Content-addressed store. One row per distinct byte-stream, ever.
CREATE TABLE IF NOT EXISTS blob (
    content_sha256 TEXT PRIMARY KEY,
    size_bytes     INTEGER NOT NULL,
    mimetype       TEXT,
    ext            TEXT,
    first_path     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_media (
    post_id        TEXT NOT NULL REFERENCES post(post_id) ON DELETE CASCADE,
    media_id       TEXT,
    url_md5        TEXT,
    source_kind    TEXT NOT NULL,
    provider       TEXT,
    content_sha256 TEXT REFERENCES blob(content_sha256),
    output_path    TEXT,
    state          TEXT NOT NULL CHECK (state IN
                     ('pending','complete','skipped_no_access','indeterminate','failed')),
    detail         TEXT,
    updated_at     TEXT NOT NULL,
    -- SQLite prohibits expressions in PRIMARY KEY / UNIQUE constraints, so the
    -- COALESCE(media_id, url_md5, output_path) identity is computed in Python by
    -- state.store -- the only module that knows the rule -- and stored here.
    dedup_key      TEXT NOT NULL,
    PRIMARY KEY (post_id, source_kind, dedup_key)
);
CREATE INDEX IF NOT EXISTS post_media_by_hash ON post_media(content_sha256);
CREATE INDEX IF NOT EXISTS post_media_by_pre  ON post_media(url_md5);

CREATE TABLE IF NOT EXISTS coverage (
    campaign_id  TEXT NOT NULL REFERENCES campaign(campaign_id),
    range_start  TEXT NOT NULL,
    range_end    TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (campaign_id, range_start)
);
