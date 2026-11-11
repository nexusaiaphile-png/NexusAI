# NexusAI Technologies

NexusAI Retail Intelligence connects point-of-sale (POS) transaction exceptions with relevant Hikvision camera evidence. POS records are the source of truth; camera events provide supporting timing and footage references.

## What NexusAI does

An authorised POS adapter sends transaction exceptions to the NexusAI API. Supported event types include:

- Refunds, returns and returns issued
- Voids and transaction voids
- Cancelled line items
- No-sale events and cash-drawer-open events
- Manager overrides and manual discounts

NexusAI records the store, till, transaction, cashier (when supplied), amount, currency and timestamp. It can correlate a POS exception with a mapped Hik-Partner Pro (HPP) camera event and attach a camera snapshot or a separately supplied video-timeline reference.

NexusAI reports exception counts and amounts by till and reporting period. It can also accept aggregated foot-traffic and invoice metrics to calculate a directional conversion-gap estimate.

## What NexusAI does not do

- It does not connect automatically to every till computer.
- It does not independently observe cash physically leaving a cash drawer or verify a payment/refund settlement.
- It does not infer refunds, voids, theft or other transaction exceptions from CCTV.
- It does not classify camera events as theft, weapons, threats, intrusion or other security incidents.
- It does not replace the retailer's POS audit trail, payment reconciliation, certified fire/security systems or store investigation procedures.

A retailer must provide an authorised POS integration/adapter that sends the relevant event data. Availability of an event type in the API does not mean a retailer's live POS is connected or currently sending that event.

## Production architecture

**Retail POS → authorised POS adapter → NexusAI POS exception API → exception database**

**Hik-Partner Pro (HPP) camera events → HPP queue consumer → POS/camera correlation → supporting camera evidence**

HPP camera events are correlated with POS exceptions before being acknowledged by the queue consumer. Camera-only events are discarded from the Retail Intelligence alert path; they are not dispatched as standalone security alerts.

## POS integration

- Endpoint: `POST /api/retail/transaction-exceptions`
- Authentication: `X-NexusAI-POS-Ingest-Token` using the server-side `NEXUSAI_POS_INGEST_TOKEN`.
- Payload: JSON matching `POSExceptionEventIn` in `backend/src/models.py`.
- Required identifiers: `site_id`, `source_event_id`, `till_id`, `exception_type`, `transaction_value`, `occurred_at`.
- `source_event_id` must be stable and unique within the site so retries are idempotent.
- Timestamps must be ISO 8601 and include an explicit timezone offset.
- The retailer/POS vendor must document whether `transaction_value` is the gross, reversed or adjusted value.

Example payload:

```json
{
  "site_id": "retailer-site-001",
  "source_event_id": "pos-event-000001",
  "till_id": "lane-04",
  "exception_type": "refund",
  "transaction_id": "receipt-12345",
  "cashier_id": "cashier-12",
  "cashier_name": "Example Cashier",
  "transaction_value": "500.00",
  "currency": "ZAR",
  "occurred_at": "2026-11-11T14:30:00+02:00",
  "camera_id": "HPP-DEVICE-SERIAL",
  "metadata": {
    "refund_reason": "customer_return",
    "payment_method": "card"
  }
}
```

Do not send customer names, card numbers, PINs or other unnecessary personal/payment credentials. Apply the retailer's POPIA and access-control requirements to all fields.

## POS integration acceptance checklist

A POS connection is not considered verified until a retailer/vendor adapter has:

1. Been authorised by the retailer and configured with the POS ingestion token.
2. Sent a test refund and a test void using the actual POS event format.
3. Received a successful API response and confirmed each event is stored once.
4. Confirmed amount semantics, timestamps, till identifiers and stable event IDs.
5. Verified camera-to-till mapping and a matching HPP event where footage correlation is required.
6. Completed a controlled end-to-end test with the retailer.

Until these checks pass, describe POS refund/void support as **API-supported, live POS connection not yet verified**.

## HPP integration

NexusAI uses Hik-Partner Pro (HPP) cloud integration. HPP device serial numbers must be mapped to the correct NexusAI site. HPP events provide camera timing/evidence for POS correlation; they are not a substitute for POS transaction data.

## Render deployment

The repository is configured for a Render Python Web Service.

- Build command: `pip install -r backend/requirements.txt`
- Start command: `uvicorn backend.src.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`
- Public site and API: served by the same FastAPI service.

## Required environment variables

Configure secrets in Render Environment; never commit their values to GitHub.

- `DATABASE_URL` — persistent Postgres/Neon connection string.
- `NEXUSAI_POS_INGEST_TOKEN` — dedicated secret for authorised POS adapters.
- HPP credentials and settings required by the HPP integration.
- Any required application/session secrets used by the deployed service.

## Local development

```bash
pip install -r backend/requirements.txt
uvicorn backend.src.main:app --reload
```
