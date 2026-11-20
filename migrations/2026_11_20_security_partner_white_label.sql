-- Security-company white-label and co-branded tenant configuration.
-- Additive only: existing retailer organisations and recipients remain unchanged.
ALTER TABLE nexusai_retail_organisations
    ADD COLUMN IF NOT EXISTS partner_id TEXT;

CREATE INDEX IF NOT EXISTS idx_retail_org_partner
    ON nexusai_retail_organisations(partner_id);

CREATE TABLE IF NOT EXISTS nexusai_partner_branding (
    partner_id TEXT PRIMARY KEY,
    brand_mode TEXT NOT NULL DEFAULT 'CO_BRANDED'
        CHECK (brand_mode IN ('FULL_WHITE_LABEL', 'CO_BRANDED')),
    brand_name TEXT NOT NULL,
    logo_url TEXT,
    primary_color TEXT NOT NULL DEFAULT '#07151d',
    accent_color TEXT NOT NULL DEFAULT '#25f5c5',
    support_email TEXT,
    custom_domain TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    CONSTRAINT fk_partner_branding_account
        FOREIGN KEY (partner_id)
        REFERENCES nexusai_partner_accounts(partner_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_branding_custom_domain
    ON nexusai_partner_branding(LOWER(custom_domain))
    WHERE custom_domain IS NOT NULL AND custom_domain <> '';

CREATE INDEX IF NOT EXISTS idx_retail_org_partner_sites
    ON nexusai_retail_organisation_sites(organisation_id, site_id);
