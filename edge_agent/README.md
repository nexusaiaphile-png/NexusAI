# NexusAI Edge Agent

The NexusAI Edge Agent runs on a computer or edge device at the customer's premises.

It performs the network-dependent work that a public Cloudflare Worker cannot safely perform against a private LAN camera.

## What it does

- Verifies the configured Hikvision camera locally.
- Uses Hikvision ISAPI with HTTP Digest authentication.
- Maintains the event-stream connection.
- Captures a local snapshot when a security event is received.
- Sends event metadata and heartbeat information to the NexusAI API.
- Automatically reconnects after network/device interruptions.
- Never sends the camera password to the public API.

## Important

The agent does not independently perform computer vision. It consumes security events produced by the Hikvision camera/NVR. Independent AI detection requires a compatible AI model or analytics engine.

## Install

1. Install Python 3.11+.
2. Open this directory.
3. Create a virtual environment: python -m venv .venv
4. Activate the environment.
5. Install dependencies: pip install -r requirements.txt
6. Copy .env.example to .env.
7. Set the site, camera IP, username and password.
8. Set a strong NEXUSAI_EDGE_TOKEN.
9. Start the agent: python agent.py

Never commit .env or real camera passwords to GitHub.

## Network model

Customer camera/NVR -> NexusAI Edge Agent -> NexusAI Cloud API -> Client Portal

The edge agent must run on a device that can reach the camera/NVR over the customer's local network.
