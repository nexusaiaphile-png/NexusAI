# NexusAI Hik-Partner Pro OpenAPI

NexusAI now has a first-party Hik-Partner Pro integration path. Security Box remains available as a fallback, but it is not required for the HPP path.

## Render environment variables

Set these as Render environment variables. Never commit them to GitHub or put them in frontend code.

- `HPP_API_BASE_URL` — the Hik-Partner Pro OpenAPI API host from the current Hikvision partner guide/account.
- `HPP_APP_KEY` — NexusAI's Hik-Partner Pro API Key/appKey.
- `HPP_SECRET_KEY` — NexusAI's Hik-Partner Pro secretKey.
- `NEXUSAI_ADMIN_KEY` — existing NexusAI Command Centre admin key.

The integration requests an access token from `POST /api/hpcgw/v1/token/get`, caches it in memory, and refreshes it when Hikvision reports an expired token.

## Current integration endpoints

All require `X-NexusAI-Admin-Key`.

- `GET /api/hpp/status`
- `POST /api/hpp/test-auth`
- `POST /api/hpp/sites/search`
- `POST /api/hpp/devices/list`
- `POST /api/hpp/cameras/list`
- `POST /api/hpp/alarms/subscribe`
- `POST /api/hpp/alarms/poll`
- `POST /api/hpp/alarms/picture-url`

The alarm polling endpoint uses Hik-Partner Pro's documented long-polling message API and normalizes JSON/XML alarm payloads into NexusAI event objects.

## First live test

1. Add the three HPP environment variables in Render.
2. Open NexusAI Command Centre.
3. Click **CHECK HPP CONNECTION**.
4. Click **TEST HPP AUTHENTICATION**.
5. Click **LOAD HIK-PARTNER PRO SITES**.
6. Once sites are visible, list the site's devices and identify an NVR/device serial.
7. Subscribe that device to alarms.
8. Poll for an authorized test alarm.

Do not expose a customer NVR to the public internet and do not put Hikvision API credentials in browser JavaScript.

## Important limitation

Hik-Partner Pro alarm messages identify the device and alarm payload. NexusAI must map device/channel information to the correct customer site before treating an event as a customer event. The current implementation is the integration foundation; site/device mapping and a continuously running alarm worker are the next production steps after the first real API test.

## Architecture

```
Hikvision NVR/cameras
        |
        v
Hik-Partner Pro
        |
        v
NexusAI Cloud
        |
        +--> NexusAI Command Centre
        |
        +--> NexusAI event engine
        |
        +--> future notification channels
```

Security Box remains a fallback for deployments where the official HPP integration cannot provide the required capability.
