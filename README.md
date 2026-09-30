# NexusAI Technologies

NexusAI Technologies — AI security technology built for what comes next.

## Production architecture

NexusAI is built around a private on-site Edge Agent and a cloud notification service.

**Hikvision camera/NVR → NexusAI Edge Agent → NexusAI Cloud → NexusAI Push Dispatcher → NexusAI app → phone notification**

The client portal is the only official installation entry point for the NexusAI mobile alert app. The app is installed as a PWA named **NexusAI**, registers a secure Web Push subscription for the client's site, and receives operating-system notifications through its service worker even when the app page is closed.

WhatsApp is not part of the NexusAI notification architecture.

## Render deployment

The repository is configured for a Render Python Web Service.

- Build command: `pip install -r backend/requirements.txt`
- Start command: `uvicorn backend.src.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`
- Public site: `/`
- Client portal: `/portal/`
- NexusAI app: `/app/`

## Required Render environment variables

Configure these in Render Environment. Never commit the values to GitHub.

- `NEXUSAI_EDGE_TOKEN` — shared authentication token for the Edge Agent.
- `DATABASE_URL` — persistent Postgres/Neon connection string.
- `NEXUSAI_VAPID_PUBLIC_KEY` — Web Push public key.
- `NEXUSAI_VAPID_PRIVATE_KEY` — Web Push private key.
- `NEXUSAI_VAPID_SUBJECT` — VAPID contact URI, normally a NexusAI HTTPS URL.
- `NEXUSAI_APP_LINK_SECRET` — long random secret used to sign portal-generated mobile installation links.

## Mobile notification model

The client taps **INSTALL NEXUSAI APP** in the portal. The cloud creates a site-bound, signed installation link. The phone opens that link, installs the NexusAI PWA, requests notification permission, creates a Web Push subscription, and sends the subscription to NexusAI Cloud.

When an Edge Agent reports a security event, the cloud stores the event and queues the push dispatcher immediately. The dispatcher sends high-urgency Web Push notifications to every registered phone for that site. It retries failed delivery with bounded exponential backoff and removes expired browser subscriptions.

The mobile service worker displays persistent operating-system notifications for events such as:

- THREAT DETECTED
- THEFT DETECTED
- WEAPON DETECTED
- INTRUSION

## Edge Agent

The `edge_agent/` application runs at the customer's premises because it needs access to private Hikvision/NVR network addresses.

It verifies the camera locally, maintains the Hikvision event stream, captures evidence snapshots, sends event metadata and heartbeat information to NexusAI Cloud, and automatically reconnects after interruptions.

Configure `NEXUSAI_API_URL=https://getnexusai.co.za` after the custom domain is active.

## Local development

```bash
pip install -r backend/requirements.txt
uvicorn backend.src.main:app --reload
```

The public site, portal, API, and NexusAI app are served by the same FastAPI service.
