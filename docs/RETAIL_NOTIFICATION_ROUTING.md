# Retail Intelligence Notification Routing

## Product decision

A qualifying retail POS exception must be reviewable in the NexusAI client dashboard and routed to both:
- the relevant store manager, limited to their assigned store/site; and
- authorised head-office recipients, limited to the branches assigned to their organisation.

The current codebase already has a web-push subscription mechanism and a WhatsApp delivery adapter. These are existing components, not proof that the complete POS-exception-to-client-notification workflow is active. The existing edge-event push path is separate from the POS exception correlation workflow. WhatsApp delivery is optional and requires provider configuration, opt-in and applicable template approval. Email delivery still needs a properly configured provider/transport.

## Required recipient model

Implement explicit, tenant-scoped notification recipients. Suggested fields:
- recipient_id (opaque stable ID)
- organisation_id / tenant_id
- site_id nullable only for head-office scope
- role: STORE_MANAGER or HEAD_OFFICE
- display_name
- email
- phone_e164 nullable
- active
- email_opt_in, push_opt_in, whatsapp_opt_in
- created_at, updated_at

A head-office recipient must have explicit authorised site assignments (many-to-many), not implicit access to every site. A store manager must have a single permitted site assignment unless a later approved role model explicitly supports more.

Before implementation, determine the authoritative tenant/organisation relationship for current `nexusai_customers` and site records. Do not infer organisation membership from matching names, email domains, or user-supplied site IDs. If the current data model cannot establish a safe organisation-to-site relationship, add the smallest explicit mapping rather than broadening access.

## Notification event and delivery model

- Create a durable notification record only after a real POS exception has been accepted and its required correlation state is known.
- Do not send alerts for arbitrary CCTV events, uncorrelated events, test payloads, or fabricated/demo data.
- Preserve the POS exception source ID as an idempotency key so retries do not create duplicate notifications.
- Store a minimal event summary, site ID, exception ID, creation timestamp, and review URL/reference. Avoid copying full cashier/customer data or footage into notification records.
- Queue channel deliveries separately (DASHBOARD, EMAIL, PUSH, WHATSAPP) with status, attempt count, provider reference, and safe error category.
- Retry transient failures with bounded exponential backoff. Do not retry permanent invalid-recipient or opt-out errors indefinitely.
- Delivery failures must never roll back accepted POS ingestion or correlation.
- Dashboard notification listing must enforce recipient/site scope server-side on every request.
- Head office may receive a summary and a secure link; detailed evidence remains behind the authenticated client dashboard and site permissions.

## Email and privacy

- Use a server-side email provider/transport configured through Render environment variables. Never place credentials in frontend code or repository files.
- Require verified recipient addresses and explicit notification preferences.
- Avoid including sensitive transaction detail, cashier identity, or footage URLs in email subject/body by default. Use a minimal alert and authenticated dashboard link.
- Respect POPIA data minimisation, access control, retention and audit requirements.
- Record recipient, channel, event ID, status, and timestamps for audit without logging secrets or full message bodies.

## User experience

### Store manager
- Receives alerts only for their assigned store.
- Dashboard shows event type, branch, till/camera reference where available, time, status, and secure review link.
- Can mark an alert as acknowledged/reviewing if the existing case workflow supports it.

### Head office
- Receives alerts only for explicitly assigned branches.
- Dashboard can filter by branch and status and provide a branch-level summary.
- Do not expose one retailer/tenant's branches or records to another tenant.

## Rollout order

1. Confirm and document the canonical organisation/site ownership model.
2. Add recipient and head-office site-assignment schema with migrations and tests.
3. Add authenticated recipient management and scoped dashboard notification API.
4. Add idempotent notification creation from the canonical POS-exception workflow.
5. Add email delivery with provider configuration, consent checks, durable queue and retries.
6. Connect existing web-push subscriptions only after mapping each subscription to an authorised recipient/role; current site-only subscriptions are not sufficient to distinguish store managers from head office.
7. Enable WhatsApp only after opt-in, sender configuration and provider/template approval.
8. Run tenant-isolation, duplicate-event, opt-out, delivery-failure and permissions tests before rollout.

## Acceptance criteria

- A store manager cannot query another store's notifications by changing a site ID.
- Head office can only see branches explicitly assigned to its organisation.
- Duplicate ingestion/retries do not create duplicate notifications.
- Notification delivery failure does not break POS ingestion.
- Email is not sent without an active, opted-in, verified recipient and configured server-side provider.
- No sensitive footage or transaction data is embedded in unprotected email.
- Dashboard and email are implemented and tested before WhatsApp is enabled.
- Existing HPP correlation and distributor flows remain unchanged.
- All work is reviewed in a feature branch and pull request; no direct merge to main.
