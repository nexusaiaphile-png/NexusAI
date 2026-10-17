-- NexusAI POS Transaction Correlation Engine
-- PostgreSQL migration aligned with backend/src/transaction_correlation.py.
-- Existing canonical footage table: nexusai_video_timeline_refs.
-- PostgreSQL IDs are BIGSERIAL/BIGINT; SQLite runtime schema uses INTEGER.
-- Additive only: does not drop or rewrite existing tables.

BEGIN;

CREATE TABLE IF NOT EXISTS nexusai_pos_exceptions (
    id                  BIGSERIAL PRIMARY KEY,
    site_id             TEXT NOT NULL,
    till_id              TEXT NOT NULL,
    exception_type      TEXT NOT NULL,
    transaction_id      TEXT,
    cashier_id          TEXT,
    cashier_name        TEXT,
    transaction_value   NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (transaction_value >= 0),
    amount              NUMERIC(14, 2) NOT NULL DEFAULT 0,
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
    metadata_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
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

CREATE TABLE IF NOT EXISTS nexusai_exception_correlations (
    id                      BIGSERIAL PRIMARY KEY,
    exception_id            BIGINT NOT NULL
                                REFERENCES nexusai_pos_exceptions(id)
                                ON DELETE CASCADE,
    source_event_id         TEXT NOT NULL,
    video_timeline_ref_id   BIGINT NOT NULL
                                REFERENCES nexusai_video_timeline_refs(id)
                                ON DELETE RESTRICT,
    correlation_type        TEXT NOT NULL DEFAULT 'POS_CAMERA',
    confidence              NUMERIC(5, 4)
                                CHECK (confidence >= 0 AND confidence <= 1),
    time_delta_ms           BIGINT,
    evidence_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_nexusai_exception_video_correlation
        UNIQUE (exception_id, video_timeline_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_nexusai_pos_exceptions_site_created
    ON nexusai_pos_exceptions (site_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_nexusai_pos_exceptions_transaction
    ON nexusai_pos_exceptions (transaction_id);

CREATE INDEX IF NOT EXISTS idx_nexusai_exception_correlations_source_event
    ON nexusai_exception_correlations (source_event_id);

CREATE INDEX IF NOT EXISTS idx_nexusai_exception_correlations_video_ref
    ON nexusai_exception_correlations (video_timeline_ref_id);

COMMIT;
