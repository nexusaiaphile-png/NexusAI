-- NexusAI POS Transaction Correlation Engine
-- PostgreSQL migration. Apply only after confirming the footage-reference table
-- is named video_timeline_refs and its primary key is id (TEXT).
-- This migration intentionally does not alter or drop existing tables.

BEGIN;

CREATE TABLE IF NOT EXISTS nexusai_pos_exceptions (
    id                  TEXT PRIMARY KEY,
    site_id             TEXT NOT NULL,
    lane_id             TEXT NOT NULL,
    transaction_id      TEXT NOT NULL,
    transaction_value   NUMERIC(12, 2) NOT NULL CHECK (transaction_value >= 0),
    cashier_id          TEXT,
    cashier_name        TEXT,
    exception_type      TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'OPEN'
                            CHECK (status IN ('OPEN', 'REVIEWING', 'RESOLVED', 'FALSE_POSITIVE')),
    source_event_id     TEXT NOT NULL UNIQUE,
    details_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nexusai_pos_exceptions_site_created
    ON nexusai_pos_exceptions (site_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_nexusai_pos_exceptions_transaction
    ON nexusai_pos_exceptions (transaction_id);

CREATE INDEX IF NOT EXISTS idx_nexusai_pos_exceptions_status
    ON nexusai_pos_exceptions (status, created_at DESC);

CREATE TABLE IF NOT EXISTS nexusai_exception_correlations (
    id                      TEXT PRIMARY KEY,
    exception_id            TEXT NOT NULL
                                REFERENCES nexusai_pos_exceptions(id)
                                ON DELETE CASCADE,
    source_event_id         TEXT NOT NULL,
    video_timeline_ref_id   TEXT NOT NULL
                                REFERENCES video_timeline_refs(id)
                                ON DELETE RESTRICT,
    correlation_type        TEXT NOT NULL DEFAULT 'POS_CAMERA',
    confidence              NUMERIC(5, 4)
                                CHECK (confidence >= 0 AND confidence <= 1),
    time_delta_ms            BIGINT,
    evidence_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_nexusai_exception_video_correlation
        UNIQUE (exception_id, video_timeline_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_nexusai_exception_correlations_source_event
    ON nexusai_exception_correlations (source_event_id);

CREATE INDEX IF NOT EXISTS idx_nexusai_exception_correlations_video_ref
    ON nexusai_exception_correlations (video_timeline_ref_id);

COMMIT;
