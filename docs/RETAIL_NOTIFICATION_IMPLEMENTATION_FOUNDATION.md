# Retail dashboard notification foundation

## What this change provides

- Explicit retailer organisation records.
- Explicit organisation-to-site membership (one organisation owns each NexusAI site).
- Store-manager and head-office recipient records without email or phone contact fields.
- Explicit recipient-to-site assignments constrained to the recipient's organisation.
- Hashed dashboard access tokens with expiry/revocation fields.
- Idempotent notification records keyed by `(site_id, source_event_id)`.
- Dashboard-only per-recipient delivery state.

## What this does not do

This migration alone does not activate customer notifications. It adds no API routes, does not send email, WhatsApp, SMS or external push notifications, does not change the existing customer dashboard, and does not alter POS ingestion or HPP correlation. The migration must not be applied to production until the current production schema has been checked and the application integration is reviewed.

## Required application rules

1. Organisation/site membership is provisioned explicitly by an authenticated NexusAI administrator.
2. A store-manager recipient must have exactly one active site assignment.
3. A head-office recipient may have multiple assignments, but every assignment must belong to the recipient's organisation.
4. Notification listing must derive allowed site IDs from the authenticated recipient's assignments on the server. Ignore/reject client-supplied organisation or site scope.
5. Recipient tokens must be high-entropy, stored only as a cryptographic hash, returned once at provisioning, and support expiry and revocation. Deliver access directly through a controlled onboarding process.
6. Create a notification only for a newly inserted canonical `nexusai_pos_exceptions` row. Use its `source_event_id` for idempotency. Do not generate alerts from generic CCTV events or uncorrelated demo payloads.
7. Notification creation must be best-effort after the POS row commits. Failure must never roll back POS ingestion.
8. The dashboard is the only notification channel. No email, WhatsApp, SMS, external push, contact details or messaging-provider configuration is in scope.
9. Do not include footage URLs, cashier identity, or unnecessary transaction detail in dashboard notification summaries.

## Migration preconditions

The migration expects the canonical `nexusai_customers(site_id)` and `nexusai_pos_exceptions(id)` tables to exist. Confirm these exact production tables and key types before applying. The migration is additive and should be applied as a reviewed, separately approved database change.

## Next implementation milestone

Build the store-manager and head-office dashboard screens, dashboard sign-in/token handling, notification list and read/acknowledgement state. Test cross-tenant isolation, one-site store managers, multi-site head-office assignments, revoked tokens, duplicate POS retries, and dashboard loading/error states. Do not add external delivery channels.
