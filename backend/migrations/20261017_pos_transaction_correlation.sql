-- NexusAI POS Transaction Correlation Engine
-- PostgreSQL migration aligned with backend/src/transaction_correlation.py.
-- Canonical relationship table: nexusai_pos_exception_video_links.
-- Canonical footage table: nexusai_video_timeline_refs.
-- PostgreSQL IDs use BIGSERIAL/BIGINT. Additive only; no data is dropped.

BEGIN;

CREATE TABLE IF NOT EXISTS nexusai_pos_exceptions (
    id                  BIGSERIAL PRIMARY KEY,
    site_id             TEXT NOT NULL,
    till_id             TEXT NOT NULL,
    exception_type      TEXT NOT NULL,
    transaction_id      TEXT,
    cashier_id          TEXT,
    cashier_name        TEXT,
    transaction_value   NUMERIC(14,2) NOT NULL DEFAULT 0,
    amount              NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'ZAR',
    occurred_at         TEXT NOT NULL,
    active_transaction  BOOLEAN,
    flagged_for_review  BOOLEAN NOT NULL DEFAULT TRUE,
    camera_id           TEXT,
    clip_start          TEXT,
    clip_end            TEXT,
    video_clip_url      TEXT,
    source_event_id     TEXT,
    correlation_status  TEXT NOT NULL DEFAULT 'pending_video_reference',
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    created_at          DOUBLE PRECISION NOT NULL,
    CONSTRAINT uq_nexusai_pos_exception_site_source_event UNIQUE (site_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS nexusai_video_timeline_refs (
    id              BIGSERIAL PRIMARY KEY,
    site_id         TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    clip_start      TEXT NOT NULL,
    clip_end        TEXT NOT NULL,
    video_clip_url  TEXT NOT NULL,
    source_ref_id   TEXT,
    created_at      DOUBLE PRECISION NOT NULL,
    CONSTRAINT uq_nexusai_video_timeline_site_source_ref UNIQUE (site_id, source_ref_id)
);

-- Match the application's existing table and column names exactly.
CREATE TABLE IF NOT EXISTS nexusai_pos_exception_video_links (
    id              BIGSERIAL PRIMARY KEY,
    exception_id    BIGINT NOT NULL,
    timeline_ref_id BIGINT,
    site_id         TEXT NOT NULL,
    camera_id       TEXT,
    clip_start      TEXT,
    clip_end        TEXT,
    video_clip_url  TEXT,
    source_ref_id   TEXT,
    match_status    TEXT NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL,
    CONSTRAINT fk_pos_video_link_exception
        FOREIGN KEY (exception_id) REFERENCES nexusai_pos_exceptions(id) ON DELETE CASCADE,
    CONSTRAINT fk_pos_video_link_timeline
        FOREIGN KEY (timeline_ref_id) REFERENCES nexusai_video_timeline_refs(id) ON DELETE RESTRICT,
    CONSTRAINT uq_nexusai_pos_exception_video_link_exception UNIQUE (exception_id)
);

CREATE INDEX IF NOT EXISTS idx_pos_exceptions_site_time
    ON nexusai_pos_exceptions (site_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_pos_exceptions_till_time
    ON nexusai_pos_exceptions (site_id, till_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_video_timeline_lookup
    ON nexusai_video_timeline_refs (site_id, camera_id, clip_start, clip_end);

CREATE INDEX IF NOT EXISTS idx_exception_video_links_site_camera
    ON nexusai_pos_exception_video_links (site_id, camera_id, clip_start, clip_end);

COMMIT;
