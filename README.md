# NexusAI Hikvision AI Security Alert Relay

NexusAI security alert relay for Hikvision cameras using the ISAPI event stream.

## Features

- Hikvision ISAPI alert-stream monitoring
- Automatic reconnection
- Event detection for intrusion, line crossing, region events, loitering, weapon, threat and motion events
- Automatic camera snapshot capture
- WhatsApp alert delivery
- Gmail alert delivery with snapshot attachment
- Environment-variable based configuration

## Setup

1. Install Python 3.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env`.
4. Fill in the real camera, WhatsApp and Gmail values in `.env`.
5. Run:

```bash
python main.py
```

## Security

Never commit `.env`, passwords, API tokens, app passwords or camera credentials to GitHub. The repository includes a `.gitignore` that excludes `.env` and runtime files.

## Important

The relay responds to events reported by the Hikvision camera. It does not itself create a new AI detection model; the exact detections available depend on the camera/NVR and its configured analytics.
