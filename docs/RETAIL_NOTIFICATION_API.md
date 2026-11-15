# Retail dashboard and phone notifications

Retailer alerts are created only from newly committed, flagged canonical POS exceptions. The dashboard is the source of truth; browser push is an optional companion so a signed-in manager or head-office user can be alerted while the dashboard is closed or another app is in use. No email, WhatsApp or SMS delivery is implemented.

## Access and branch isolation
- CEO-administered retailer organisations and branch assignments.
- Store managers are assigned exactly one branch.
- Head-office recipients see only explicitly assigned branches.
- Recipient access tokens are high entropy, stored as SHA-256 hashes, returned once and expire after 90 days.
- All notification reads and read acknowledgements are scoped server-side to the recipient's branch assignments.

## Dashboard
- `GET /api/retail/notifications/me` returns recipient identity and assigned branches.
- `GET /api/retail/notifications` returns notifications for assigned branches.
- `POST /api/retail/notifications/{notification_id}/read` marks only an assigned notification as read.
- Mobile dashboard: `/app/retail.html`. The recipient signs in with the one-time access token.

## Browser push
- `GET /api/retail/push/config` exposes the VAPID public key only when all server-side VAPID settings are configured.
- `POST /api/retail/push/subscribe` stores a browser subscription against the authenticated recipient.
- `DELETE /api/retail/push/subscribe` revokes the current device subscription.
- Push is sent asynchronously after the POS exception and dashboard notification commit. It never blocks or rolls back POS ingestion.
- Push payloads contain only a generic alert and branch name; no transaction amount, cashier identity, footage URL or sensitive POS detail.
- Expired browser endpoints (HTTP 404/410) are revoked.
- Push requires HTTPS, browser notification permission, a supported browser, and configured `NEXUSAI_VAPID_PUBLIC_KEY`, `NEXUSAI_VAPID_PRIVATE_KEY`, and `NEXUSAI_VAPID_SUBJECT` Render environment variables.
- On iPhone/iPad, web push generally requires a supported iOS version and the web app added to the Home Screen. Browser/device support varies.
- Push subscriptions persist after closing the dashboard. Signing out in the NexusAI retail page revokes that device subscription. Recipient access tokens expire after 90 days and may need re-issuing.

## Privacy and delivery
- No email address or phone number is collected for this feature.
- No WhatsApp, email, SMS, Discord or third-party messaging integration is used for retailer alerts.
- Push delivery is best-effort; dashboard notification records remain available even if push is unavailable or fails.
- Only the assigned manager/head-office recipients receive a push for their branch.
- POS exception ingestion remains independent from push delivery.

## Deployment
The SQL migration is additive and must be reviewed and applied separately after confirming the production schema. This PR does not apply database changes or configure Render secrets. Do not invite customer users until migration, VAPID configuration, production smoke tests and account provisioning are complete.
