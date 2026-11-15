# Retail notification implementation foundation

## Scope
This feature provides secure retailer branch assignment, dashboard notifications, and optional browser push alerts. The dashboard is the source of truth; push lets a recipient receive an OS/browser notification while the dashboard is closed. No email, WhatsApp or SMS channels or recipient contact details are in scope.

## Security rules
1. Organisation and site membership are provisioned explicitly by an authenticated NexusAI administrator.
2. Store managers have exactly one active site assignment; head-office recipients have only explicitly assigned sites.
3. Notification reads and acknowledgements derive allowed site IDs from the authenticated recipient on the server.
4. Recipient access tokens are high entropy, hashed at rest, returned once, expire after 90 days and can be revoked.
5. Browser push subscriptions are linked to the authenticated recipient, endpoint-unique, and revocable on sign-out.
6. Create notifications only for newly committed canonical POS exceptions, idempotent by (site_id, source_event_id).
7. Notification creation and push dispatch are best-effort after POS commit. Push failures never roll back POS ingestion.
8. Push payloads must not contain footage URLs, cashier identity, transaction amount or unnecessary POS detail.
9. No email, WhatsApp, SMS or external messaging provider configuration is permitted.

## Operational requirements
- Apply the additive SQL migration only after confirming production schema and reviewing its foreign keys.
- Configure VAPID public/private key and subject in Render environment variables before enabling push.
- HTTPS and browser permission are required; iOS web push requires supported iOS and Home Screen installation.
- Test manager single-branch isolation, head-office assigned-branch isolation, invalid/expired/revoked tokens, subscription ownership conflicts, unsubscribe, expired endpoints, duplicate POS retries, and push failure isolation.
- Do not claim production notifications are live until migration, VAPID setup, account provisioning and end-to-end tests are complete.
