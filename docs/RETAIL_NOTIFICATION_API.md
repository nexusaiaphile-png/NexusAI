# Retail dashboard notification API

This implementation provides CEO-administered retailer organisation and branch provisioning, single-branch store-manager access, multi-branch head-office access, one-time high-entropy recipient tokens (SHA-256 hashes stored server-side; 90-day expiry), server-scoped dashboard notification listing, and best-effort notification creation from newly persisted POS exceptions.

## Dashboard-only scope

The dashboard is the sole customer notification channel. This feature does not collect recipient email addresses or phone numbers and does not implement email, WhatsApp, SMS, or external push delivery.

## API
- `POST /api/admin/retail/organisations` — bearer admin PIN session; registers existing NexusAI customer site IDs to one organisation.
- `POST /api/admin/retail/organisations/{organisation_id}/recipients` — creates STORE_MANAGER or HEAD_OFFICE recipient and returns a one-time dashboard access token.
- `GET /api/retail/notifications/me` — recipient identity and explicitly assigned branches.
- `GET /api/retail/notifications` — dashboard notifications only for recipient's server-side site assignments.

## Safety
- Store managers are constrained to exactly one branch.
- Head-office assignments must belong to the same organisation.
- Recipient tokens are returned once, stored only as SHA-256 hashes, expire after 90 days, and are revocable by setting revoked_at.
- Client-supplied site IDs are never used to scope notification reads.
- Only flagged canonical POS exceptions create alerts; duplicate source_event_id values are idempotent.
- Notification creation is best-effort after the POS exception commits. Failure cannot undo the POS event.
- Only DASHBOARD delivery records can be created by the schema.
- The notification text contains no cashier identity, transaction amount or footage URL.

## Deployment
The SQL migration must be applied separately after confirming production schema. This code does not run that migration automatically. No production migration, secrets or customer invitations are performed by this pull request.
