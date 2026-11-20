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
