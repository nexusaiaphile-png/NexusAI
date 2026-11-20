# Security partner white-label foundation

This additive migration establishes per-security-company branding configuration for NexusAI Retail Intelligence.

## Modes

- `FULL_WHITE_LABEL`: partner brand is the customer-facing product identity. NexusAI is not shown in ordinary dashboard chrome. Required legal, privacy and data-processing disclosures must still accurately identify the responsible parties and technology/data processors.
- `CO_BRANDED`: partner brand remains primary, with a discreet "Powered by NexusAI" attribution.

## Configuration scope

Brand configuration is keyed to a verified `nexusai_partner_accounts.partner_id`, not a user-supplied company name. A retailer organisation may be linked to one security partner through `nexusai_retail_organisations.partner_id`. This prevents branding from being selected by an untrusted browser parameter.

## Fields

- `brand_name`: customer-facing service name.
- `logo_url`: approved HTTPS logo asset URL.
- `primary_color`, `accent_color`: validated hex colours.
- `support_email`: partner support contact.
- `custom_domain`: reserved for a later DNS/TLS onboarding phase; it must not be treated as active until ownership and certificate provisioning are verified.
- `enabled`: disables partner branding and falls back to NexusAI defaults.

## Security requirements for application wiring

1. Resolve branding from the authenticated retailer recipient's organisation and its partner_id. Never trust a partner ID supplied by the browser to determine tenant branding.
2. Keep notification queries, branch assignments, push subscriptions and audit records scoped by existing recipient/organisation permissions. Branding is presentation only and grants no data access.
3. Do not suppress legally required privacy notices, data-controller/processor information or contractual disclosures in full white-label mode.
4. Only NexusAI administrators may assign partner_id to an organisation and update partner branding until a separately permissioned partner-admin workflow is reviewed.
5. A custom domain remains inactive until domain control, TLS and host-to-tenant resolution are implemented and tested.
6. Do not activate or imply that any partner has a branded live tenant until the application endpoints and dashboard integration are deployed and validated.


## Stage 1: NexusAI admin controls

The admin Command Centre now has a Security Partner White-Label Administration section.

- It lists only active security partner accounts.
- It lists active retailer organisations and their current partner assignment.
- NexusAI administrators can assign or unassign an organisation to an active partner.
- NexusAI administrators can save each active partner's mode, display name, HTTPS logo URL, primary/accent colours and support email.
- API access uses the existing authenticated NexusAI administrator bearer session. The browser cannot select a partner's identity for access control; the admin action is explicitly privileged.
- Branding settings are presentation metadata only. They do not grant access to retailer notifications, branches or footage.
- Custom domains are not configurable or active in this stage.
- The admin interface explicitly warns that saved settings do not change the retailer dashboard until the dashboard-branding stage is implemented.

Endpoints:
- `GET /api/admin/retail/white-label/partners`
- `GET /api/admin/retail/white-label/organisations`
- `POST /api/admin/retail/white-label/assign`
- `POST /api/admin/retail/white-label/branding`

All four endpoints require the existing administrator bearer token. Active partner status is checked server-side before assignment or branding updates.
