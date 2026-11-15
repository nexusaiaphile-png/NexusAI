# Retail notification routing — implementation foundation

## What this change provides

This pull request adds an additive PostgreSQL schema foundation for the retail notification feature:

- Explicit retailer organisation records.
- Explicit organisation-to-site membership (one organisation owns each NexusAI site).
- Store-manager and head-office recipient records.
- Explicit recipient-to-site assignments constrained to the recipient's organisation.
- Hashed recipient access tokens with expiry/revocation fields.
- Idempotent notification records keyed by `(site_id, source_event_id)`.
- Per-recipient/channel delivery state for dashboard, email, push and WhatsApp.

## What this change does not do

This migration alone does not activate customer notifications. It adds no API routes, does not send email/WhatsApp/push, does not change the existing customer dashboard, and does not alter POS ingestion or HPP correlation. The migration must not be applied to production until the current production schema has been checked and the application integration is reviewed.

## Required application rules

1. Organisation/site membership is provisioned explicitly by an authenticated NexusAI administrator. Never infer it from a company name, email domain, or contact details.
2. A store-manager recipient must have exactly one active site assignment. Enforce this in the recipient-management service and test it.
3. A head-office recipient may have multiple assignments, but every assignment must belong to the recipient's organisation.
4. Notification listing must derive allowed site IDs from the authenticated recipient's assignments on the server. Ignore/reject client-supplied organisation or site scope.
5. Recipient tokens must be high-entropy, stored only as a cryptographic hash, returned once at provisioning, and support expiry and revocation.
6. Create a notification only for a newly inserted canonical `nexusai_pos_exceptions` row. Use its `source_event_id` for idempotency. Do not generate alerts from generic CCTV events or uncorrelated demo payloads.
7. Notification creation and delivery must be best-effort after the POS row commits. Delivery failures must never roll back POS ingestion.
8. Email/WhatsApp/push remain disabled until provider configuration, recipient opt-in, and delivery tests are in place. Dashboard delivery is the first channel.
9. Do not include footage URLs, cashier identity, or unnecessary transaction detail in email or push content.

## Migration preconditions

The migration expects the canonical `nexusai_customers(site_id)` and `nexusai_pos_exceptions(id)` tables to exist. Confirm these exact production tables and key types before applying. The migration is additive and should be applied as a reviewed, separately approved database change.

## Next implementation milestone

Add the repository/service layer, admin provisioning endpoints, recipient-token authentication, server-side scoped notification listing, and tests for cross-tenant isolation, one-site store managers, multi-site head office assignments, revoked tokens, duplicate POS retries, and delivery failure isolation. Then wire notification creation into the canonical POS exception insert path and add the client dashboard view. Email follows only after the dashboard path passes tests.
