# NexusAI DevOps Operator Bot — Safe Mode

This is an isolated Discord application for read-only monitoring of one Render service. It does not change service settings, deployments, environment variables, source code, or database records.

## Commands

- `/status` — reads service metadata from the Render API.
- `/deploys` — reads the latest deployment records.
- `/diagnose` — reports only checks actually performed. It does not fabricate POS, HPP, database, or latency metrics.

All commands are restricted to the numeric Discord user ID configured in `NEXUSAI_DISCORD_OWNER_ID`. The bot fails closed if required settings are missing. Responses are ephemeral to the authorized operator.

## Deploy as a separate Render Background Worker

Do not replace or modify the existing NexusAI web service or HPP worker. Create a separate Background Worker from this repository with:

- **Root Directory:** `operator_bot`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python bot.py`

Add these environment variables in the new worker's Render Environment panel:

- `NEXUSAI_DISCORD_BOT_TOKEN` — Discord bot token; keep secret.
- `NEXUSAI_DISCORD_OWNER_ID` — your numeric Discord user ID, not your username.
- `RENDER_API_KEY` — a Render API key with only the permissions required to read the selected service.
- `RENDER_SERVICE_ID` — the exact Render service ID to monitor.

Never commit these values to GitHub or send them in Discord. If a credential has been exposed, rotate it.

## Discord setup

1. Create an application and bot in the Discord Developer Portal.
2. Enable the bot's application commands and install it in a private server where only you have access.
3. Invite it with the `bot` and `applications.commands` scopes. It does not require the Message Content intent.
4. Set the four environment variables above in the new Render worker.
5. Run `/status`, `/deploys`, and `/diagnose` in the private server.

## Important limits

- Render service metadata and deployment history are not proof that the application, database, POS intake, HPP integration, or cameras are healthy.
- This first version intentionally does not fetch runtime logs, modify environment variables, onboard stores, patch code, merge pull requests, or trigger deploys.
- A Render Background Worker is a separate service and may require a paid Render plan depending on current plan availability. Confirm the cost in Render before creating it.
- Deploy this branch as a separate worker only after reviewing the pull request. Existing NexusAI services are not changed by these files.
