-- Government Watchdog Phase 1 schema (WEI-255 / WEI-258).
-- See Docs/phase1-spec.md §5. Idempotent: safe to re-run on the same DB.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    source_url      TEXT NOT NULL UNIQUE,
    referer_url     TEXT,
    title           TEXT,
    doc_type        TEXT,
    doc_date        TEXT,
    local_path      TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER,
    fetch_time_utc  TEXT NOT NULL,
    wayback_url     TEXT,
    cms_signature   TEXT,
    robots_status   TEXT,
    raw_text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_doc_date ON documents(doc_date);
CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);

CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL UNIQUE,
    video_url       TEXT NOT NULL,
    channel_id      TEXT,
    channel_title   TEXT,
    upload_date     TEXT,
    meeting_date    TEXT,
    duration_seconds INTEGER,
    language        TEXT,
    segment_count   INTEGER,
    full_text       TEXT NOT NULL,
    timestamped_text TEXT,
    local_path      TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    fetch_time_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_meeting_date ON transcripts(meeting_date);

CREATE TABLE IF NOT EXISTS meetings (
    id              INTEGER PRIMARY KEY,
    meeting_date    TEXT NOT NULL,
    body            TEXT NOT NULL,
    title           TEXT,
    source_url      TEXT,
    transcript_id   INTEGER REFERENCES transcripts(id),
    notes           TEXT,
    fetch_time_utc  TEXT NOT NULL,
    UNIQUE (meeting_date, body)
);

CREATE TABLE IF NOT EXISTS meeting_documents (
    meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role            TEXT,
    PRIMARY KEY (meeting_id, document_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY,
    object_type     TEXT NOT NULL,
    object_id       INTEGER NOT NULL,
    chunk_index     INTEGER NOT NULL,
    chunk_text      TEXT NOT NULL,
    model           TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    vector          BLOB NOT NULL,
    embed_time_utc  TEXT NOT NULL,
    UNIQUE (object_type, object_id, chunk_index, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_object ON embeddings(object_type, object_id);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY,
    started_utc     TEXT NOT NULL,
    finished_utc    TEXT,
    status          TEXT NOT NULL,
    targets         TEXT NOT NULL,
    new_documents   INTEGER DEFAULT 0,
    new_transcripts INTEGER DEFAULT 0,
    notes           TEXT
);
