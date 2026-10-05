# NexusAI Hik-Partner Pro OpenAPI

NexusAI's primary Hikvision integration is now built around the Hik-Partner Pro OpenAPI V2.15.500 guide supplied to the project. Security Box remains a fallback only.

## Server environment

Set these on Render. Never commit them or expose them in browser code.

- `HPP_APP_KEY` — NexusAI Hik-Partner Pro API Key/appKey.
- `HPP_SECRET_KEY` — NexusAI Hik-Partner Pro secretKey.
- `NEXUSAI_ADMIN_KEY` — existing NexusAI Command Centre admin key.
- `HPP_ALARM_WORKER_ENABLED` — optional; defaults to `true`.

Do **not** configure `HPP_API_BASE_URL`. The HPP guide requires NexusAI to obtain a token from the common token domain and then use the returned regional `areaDomain` for service APIs.

## HPP flow implemented

```
HPP appKey + secretKey
        |
        v
POST /api/hpcgw/v1/token/get
        |
        +--> accessToken
        +--> expireTime
        +--> areaDomain
                    |
                    v
        HPP site/device APIs
                    |
                    v
        /api/hpcgw/v1/mq/subscribe
                    |
                    v
        /api/hpcgw/v1/mq/messages
              (20-second long poll)
                    |
                    v
              data.list + batchId
                    |
                    v
        NexusAI device mapping
                    |
                    v
        persist_event() + notifications
                    |
                    v
        POST /api/hpcgw/v1/mq/offset
```

The uploaded guide specifically says the messages API uses long polling, recommends calling it continuously, and requires the returned batch ID to be acknowledged so messages are not re-sent.

## Current API routes

All HPP admin routes require `X-NexusAI-Admin-Key`.

- `GET /api/hpp/status`
- `POST /api/hpp/test-auth`
- `POST /api/hpp/sites/search`
- `POST /api/hpp/devices/list`
- `POST /api/hpp/cameras/list`
- `POST /api/hpp/alarms/subscribe`
- `POST /api/hpp/alarms/poll`
- `POST /api/hpp/alarms/offset`
- `POST /api/hpp/alarm/picture-url`
- `GET /api/hpp/mappings`
- `POST /api/hpp/mappings`

## Customer mapping

HPP alarm messages identify the Hikvision device serial. NexusAI therefore maps:

```
Hikvision device serial
        ↓
NexusAI HPP mapping
        ↓
NexusAI customer site_id
        ↓
existing NexusAI event database
        ↓
existing NexusAI notification pipeline
```

A device must be mapped before its production alarm batch is acknowledged. This prevents an unmapped customer alarm from being silently discarded.

Example mapping body:

```json
{
  "hpp_site_id": "",
  "hpp_device_serial": "HIKVISION_DEVICE_SERIAL",
  "nexusai_site_id": "site-XXXXXXXXXXXX"
}
```

## What was removed from the HPP path

- The old fixed `HPP_API_BASE_URL` requirement.
- The old assumption that alarm messages arrive directly as `data` instead of `data.list`.
- The old omission of `/mq/offset`.
- The old one-shot-only alarm handling.

Security Box code/routes were **not** deleted because it is still a controlled fallback for Hikvision devices or deployments where HPP cannot provide a required capability.

## Production architecture

```
Hikvision NVR / cameras
          |
          v
   Hik-Partner Pro
          |
          v
    NexusAI Cloud
       /       \
      v         v
Command Centre  Event/Notification Engine
```

HPNetSDK is kept separate from the Render FastAPI process. Hikvision documents HPNetSDK for functions such as live view/playback/download, while HPP OpenAPI handles site/device management and alarms.

## First live test

1. Put `HPP_APP_KEY`, `HPP_SECRET_KEY`, and the existing `NEXUSAI_ADMIN_KEY` into Render.
2. Deploy.
3. Call **TEST HPP AUTHENTICATION**.
4. Confirm NexusAI receives a regional `areaDomain`.
5. Load HPP sites.
6. Load devices and identify the Hikvision device serial.
7. Create the device-serial → NexusAI site mapping.
8. Subscribe the device to alarms.
9. Trigger one authorized Hikvision test event.
10. Confirm the event appears in NexusAI.
11. Confirm the batch is acknowledged.
12. Confirm the existing NexusAI notification path receives the event.

Do not put HPP credentials in customer browsers and do not expose customer NVRs through public port forwarding. Hikvision's own HPP integration material describes the OpenAPI as the cloud integration layer and highlights device management and alarm subscription capabilities.
