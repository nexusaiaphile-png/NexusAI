# NexusAI DevOps Operator Bot — Advanced Read-Only Mode

This is a separate Discord application for owner-only operational checks. It can read metadata from one Render service and repository metadata from GitHub. It can optionally send test alerts to a Discord webhook. It does not change service settings, deployments, environment variables, source code, or database records.

## Commands

- `/status` — reads service metadata from the configured Render service.
- `/deploys` — reads recent deployment records.
- `/diagnose` — reports the Render checks actually performed and optionally checks GitHub API access.
- `/github` — reads repository metadata and recent commits on the configured operator branch.
- `/test_alert` — sends a test embed to an optional Discord webhook.
- `/simulate_crash` — tests the alert pipeline with a mock incident only; it does not monitor real worker crashes, touch production transactions, or modify code.

All commands are restricted to the numeric Discord user ID configured in `NEXUSAI_DISCORD_OWNER_ID`. The bot fails closed if required settings are missing. Command responses are ephemeral.

## Deploy as a separate Render Background Worker

Do not replace or modify the existing NexusAI web service, production Worker, or Hikvision integration. Use the existing operator worker or create a separate Background Worker from this repository with:

- **Root Directory:** `operator_bot`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python bot.py`
- **Branch:** `feature/devops-operator-safe-mode`

## Required environment variables

Set these in the Render operator worker's Environment panel. Never commit them to GitHub or send them in Discord.

- `NEXUSAI_DISCORD_BOT_TOKEN` — Discord bot token.
- `NEXUSAI_DISCORD_OWNER_ID` — numeric Discord user ID, not username.
- `RENDER_API_KEY` — Render API key with only the permissions needed to read the target service.
- `RENDER_SERVICE_ID` — exact ID of the Render service to inspect.

## Optional environment variables

- `GITHUB_TOKEN` — GitHub token with read-only access to the repository for `/github` and GitHub checks in `/diagnose`. Do not grant write permissions for this version.
- `GITHUB_REPO_OWNER` — defaults to `nexusaiaphile-png`.
- `GITHUB_REPO_NAME` — defaults to `NexusAI`.
- `GITHUB_BRANCH` — defaults to `feature/devops-operator-safe-mode`.
- `DISCORD_WEBHOOK_URL` — optional Discord webhook URL for test alerts. Keep it secret.
- `LOG_LEVEL` — optional Python logging level; defaults to `INFO`.

## Discord setup

1. Install the Discord application with the `bot` and `applications.commands` scopes.
2. Message Content intent is not required for these slash commands.
3. Configure the required environment variables above in Render.
4. Redeploy and check Render logs for successful command synchronization.
5. Test `/status`, `/deploys`, `/diagnose`, and `/github`. Use `/test_alert` only after configuring the webhook.

## Safety and limitations

- Render metadata and deployment history do not prove application, database, POS intake, HPP, or camera health.
- The bot does not yet retrieve Render runtime logs or receive live crash events from the separate NexusAI Worker.
- `/simulate_crash` is a mock alert test, not a production crash interceptor.
- This version does not patch code, commit to GitHub, merge pull requests, modify environment variables, onboard stores, or trigger deployments. Future repair features should create a branch, run tests, and open a pull request for owner approval.
- A Render Background Worker may require a paid plan depending on current Render plan availability. Confirm the current cost in Render.
- If a credential has been exposed, revoke and rotate it.
