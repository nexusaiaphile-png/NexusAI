-- NexusAI Retail Intelligence notification foundation.
-- Explicit organisation/site assignments; no inference from email domains or names.
-- Additive only. Apply only after review and verification of the production schema.

BEGIN;

CREATE TABLE IF NOT EXISTS nexusai_retail_organisations (
    organisation_id TEXT PRIMARY KEY,
    organisation_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS nexusai_retail_organisation_sites (
    organisation_id TEXT NOT NULL REFERENCES nexusai_retail_organisations(organisation_id) ON DELETE CASCADE,
    site_id TEXT NOT NULL REFERENCES nexusai_customers(site_id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (organisation_id, site_id),
    UNIQUE (site_id)
);

CREATE TABLE IF NOT EXISTS nexusai_retail_notification_recipients (
    recipient_id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES nexusai_retail_organisations(organisation_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('STORE_MANAGER', 'HEAD_OFFICE')),
    display_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone_e164 TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    dashboard_opt_in BOOLEAN NOT NULL DEFAULT TRUE,
    email_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS nexusai_retail_recipient_sites (
    recipient_id TEXT NOT NULL REFERENCES nexusai_retail_notification_recipients(recipient_id) ON DELETE CASCADE,
    organisation_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (recipient_id, site_id),
    FOREIGN KEY (organisation_id, site_id)
        REFERENCES nexusai_retail_organisation_sites(organisation_id, site_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nexusai_retail_notification_tokens (
    token_hash TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL REFERENCES nexusai_retail_notification_recipients(recipient_id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION,
    revoked_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS nexusai_retail_notifications (
    notification_id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL REFERENCES nexusai_retail_organisations(organisation_id) ON DELETE CASCADE,
    site_id TEXT NOT NULL,
    pos_exception_id BIGINT NOT NULL REFERENCES nexusai_pos_exceptions(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (organisation_id, site_id)
        REFERENCES nexusai_retail_organisation_sites(organisation_id, site_id) ON DELETE CASCADE,
    UNIQUE (site_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS nexusai_retail_notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES nexusai_retail_notifications(notification_id) ON DELETE CASCADE,
    recipient_id TEXT NOT NULL REFERENCES nexusai_retail_notification_recipients(recipient_id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('DASHBOARD', 'EMAIL', 'PUSH', 'WHATSAPP')),
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'QUEUED', 'SENT', 'FAILED', 'SKIPPED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_reference TEXT,
    error_code TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (notification_id, recipient_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_retail_recipient_sites_site
    ON nexusai_retail_recipient_sites(site_id, recipient_id);
CREATE INDEX IF NOT EXISTS idx_retail_notifications_site_created
    ON nexusai_retail_notifications(site_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retail_notifications_org_created
    ON nexusai_retail_notifications(organisation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retail_deliveries_status_created
    ON nexusai_retail_notification_deliveries(status, created_at);

COMMIT;
