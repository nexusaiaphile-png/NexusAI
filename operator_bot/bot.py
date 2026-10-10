import logging
import os
from datetime import datetime, timezone
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("nexusai_operator")

DISCORD_TOKEN = os.getenv("NEXUSAI_DISCORD_BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("NEXUSAI_DISCORD_OWNER_ID", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
GITHUB_REPO_OWNER = os.getenv("GITHUB_REPO_OWNER", "nexusaiaphile-png").strip()
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME", "NexusAI").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "feature/devops-operator-safe-mode").strip()
RENDER_API_BASE = "https://api.render.com/v1"
GITHUB_API_BASE = "https://api.github.com"

required = [
    name for name, value in (
        ("NEXUSAI_DISCORD_BOT_TOKEN", DISCORD_TOKEN),
        ("NEXUSAI_DISCORD_OWNER_ID", OWNER_ID_RAW),
        ("RENDER_API_KEY", RENDER_API_KEY),
        ("RENDER_SERVICE_ID", RENDER_SERVICE_ID),
    ) if not value
]
if required:
    raise RuntimeError("Missing required environment variables: " + ", ".join(required))
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("NEXUSAI_DISCORD_OWNER_ID must be a numeric Discord user ID.") from exc
if OWNER_ID <= 0:
    raise RuntimeError("NEXUSAI_DISCORD_OWNER_ID must be a positive Discord user ID.")

intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def owner_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        allowed = interaction.user is not None and interaction.user.id == OWNER_ID
        if not allowed:
            log.warning("Denied operator command for Discord user ID %s", getattr(interaction.user, "id", "unknown"))
            if not interaction.response.is_done():
                await interaction.response.send_message("Not authorized.", ephemeral=True)
        return allowed
    return app_commands.check(predicate)


async def render_get(path: str, params: dict[str, str] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{RENDER_API_BASE}{path}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()


async def github_get(path: str, params: dict[str, str] | None = None) -> Any:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{GITHUB_API_BASE}{path}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()


def short(value: Any, limit: int = 900) -> str:
    text = str(value if value is not None else "Unknown")
    return text[:limit] + ("…" if len(text) > limit else "")


def timestamp(value: Any) -> str:
    if not value:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return short(value, 80)


async def send_alert(title: str, description: str, color: int = 0x3498DB) -> bool:
    """Send an optional Discord webhook alert. Returns False if not configured or delivery fails."""
    if not DISCORD_WEBHOOK_URL:
        log.info("Alert not sent because DISCORD_WEBHOOK_URL is not configured.")
        return False
    payload = {
        "embeds": [{
            "title": short(title, 240),
            "description": short(description, 3500),
            "color": color,
            "footer": {"text": "NexusAI Operator • Safe Mode"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.post(DISCORD_WEBHOOK_URL, json=payload)
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("Discord webhook delivery failed: %s", type(exc).__name__)
        return False


@bot.event
async def on_ready() -> None:
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s application commands; connected as %s", len(synced), bot.user)
    except Exception:
        log.exception("Could not sync Discord application commands")
    log.info("NexusAI Operator online in SAFE MODE.")


@bot.tree.command(name="status", description="Read the configured Render service status.")
@owner_check()
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}")
        service = data.get("service", data) if isinstance(data, dict) else {}
        details = service.get("serviceDetails", {})
        url = details.get("url") if isinstance(details, dict) else None
        embed = discord.Embed(title="NexusAI Render Status", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Service", value=short(service.get("name", "Unknown service"), 200), inline=True)
        embed.add_field(name="Type", value=short(service.get("type", "Unknown"), 80), inline=True)
        suspended = service.get("suspended")
        embed.add_field(name="Suspension", value=("Suspended" if suspended is True else "Not marked suspended" if suspended is False else "Not provided by API"), inline=True)
        embed.add_field(name="Service URL", value=short(url or "Not provided by Render API", 300), inline=False)
        embed.set_footer(text="Read-only metadata; this does not prove application or database health.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        log.warning("Render status request failed with HTTP %s", exc.response.status_code)
        await interaction.followup.send(f"Render API returned HTTP {exc.response.status_code}. Check the API key permissions and service ID.", ephemeral=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Render status request failed: %s", type(exc).__name__)
        await interaction.followup.send("Could not reach or parse Render API. Check the worker logs; secrets are not shown.", ephemeral=True)


@bot.tree.command(name="deploys", description="List recent deployments for the configured Render service.")
@owner_check()
async def deploys(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}/deploys", {"limit": "5"})
        rows = data if isinstance(data, list) else data.get("deploys", []) if isinstance(data, dict) else []
        embed = discord.Embed(title="Recent NexusAI Deployments", color=discord.Color.blurple())
        if not rows:
            embed.description = "No deployment records found in the expected response format."
        for item in rows[:5]:
            record = item.get("deploy", item) if isinstance(item, dict) else {}
            commit = record.get("commit", {})
            if not isinstance(commit, dict):
                commit = {}
            embed.add_field(
                name=short(record.get("status", "Unknown status"), 100),
                value=f"Created: {timestamp(record.get('createdAt'))}\nUpdated: {timestamp(record.get('updatedAt'))}\nCommit: {short(commit.get('id') or record.get('commitId') or 'Not provided', 100)}",
                inline=False,
            )
        embed.set_footer(text="Read-only deployment history; this command does not deploy or roll back.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        log.warning("Render deployments request failed with HTTP %s", exc.response.status_code)
        await interaction.followup.send(f"Render API returned HTTP {exc.response.status_code}. Check API permissions and service ID.", ephemeral=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Render deployments request failed: %s", type(exc).__name__)
        await interaction.followup.send("Could not retrieve deployments. Check the operator worker logs.", ephemeral=True)


@bot.tree.command(name="diagnose", description="Run real read-only Render and GitHub checks.")
@owner_check()
async def diagnose(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    checks: list[tuple[str, str]] = []
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}")
        service = data.get("service", data) if isinstance(data, dict) else {}
        checks.append(("Render API", "PASS — service metadata returned" if service else "UNKNOWN — empty/unexpected response"))
        checks.append(("Render service", short(service.get("name", "Not provided"), 200)))
        checks.append(("Service type", short(service.get("type", "Not provided"), 100)))
        checks.append(("Suspended flag", str(service.get("suspended", "Not provided"))))
    except httpx.HTTPStatusError as exc:
        checks.append(("Render API", f"FAIL — HTTP {exc.response.status_code}"))
    except (httpx.HTTPError, ValueError) as exc:
        checks.append(("Render API", f"FAIL — {type(exc).__name__}"))

    if GITHUB_TOKEN:
        try:
            repo = await github_get(f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
            checks.append(("GitHub API", "PASS — repository metadata returned"))
            checks.append(("Repository", short(repo.get("full_name", "Not provided"), 200)))
            checks.append(("Default branch", short(repo.get("default_branch", "Not provided"), 100)))
        except httpx.HTTPStatusError as exc:
            checks.append(("GitHub API", f"FAIL — HTTP {exc.response.status_code}"))
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            checks.append(("GitHub API", f"FAIL — {type(exc).__name__}"))
    else:
        checks.append(("GitHub API", "NOT CHECKED — optional GITHUB_TOKEN is not configured"))

    embed = discord.Embed(title="NexusAI Safe Diagnostics", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    for label, result in checks:
        embed.add_field(name=label, value=short(result, 500), inline=False)
    embed.add_field(name="Not checked", value="Runtime application logs, database, POS intake, Hik-Partner Pro event delivery, camera events, and latency.", inline=False)
    embed.set_footer(text="Read-only checks only. No code is changed and no health metrics are invented.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="github", description="Read repository metadata and recent commit information.")
@owner_check()
async def github_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not GITHUB_TOKEN:
        await interaction.followup.send("GitHub check is not configured. Add a read-only GITHUB_TOKEN to the operator worker's environment.", ephemeral=True)
        return
    try:
        repo = await github_get(f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
        commits = await github_get(
            f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/commits",
            {"sha": GITHUB_BRANCH, "per_page": "3"},
        )
        embed = discord.Embed(title="NexusAI GitHub Repository", color=discord.Color.dark_grey(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Repository", value=short(repo.get("full_name", "Unknown"), 200), inline=False)
        embed.add_field(name="Default branch", value=short(repo.get("default_branch", "Unknown"), 100), inline=True)
        embed.add_field(name="Operator branch", value=short(GITHUB_BRANCH, 150), inline=True)
        lines = []
        if isinstance(commits, list):
            for item in commits[:3]:
                commit = item.get("commit", {}) if isinstance(item, dict) else {}
                lines.append(f"• {short(item.get('sha', '')[:7], 10)} — {short(commit.get('message', 'No message').splitlines()[0], 160)}")
        embed.add_field(name="Recent branch commits", value="\n".join(lines) if lines else "No commit records returned.", inline=False)
        embed.set_footer(text="Read-only GitHub access; no repository changes are made.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        log.warning("GitHub request failed with HTTP %s", exc.response.status_code)
        await interaction.followup.send(f"GitHub API returned HTTP {exc.response.status_code}. Check token access and repository settings.", ephemeral=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("GitHub request failed: %s", type(exc).__name__)
        await interaction.followup.send("Could not retrieve GitHub information. Check the operator worker logs.", ephemeral=True)


@bot.tree.command(name="test_alert", description="Send a test alert to the configured Discord webhook.")
@owner_check()
async def test_alert(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    delivered = await send_alert("NexusAI Operator Test Alert", "This is a manually requested test. It does not indicate a production incident.", 0x3498DB)
    if delivered:
        await interaction.followup.send("Test alert delivered to the configured webhook.", ephemeral=True)
    else:
        await interaction.followup.send("Alert not delivered. Configure DISCORD_WEBHOOK_URL and check worker logs; no secret is displayed.", ephemeral=True)


@bot.tree.command(name="simulate_crash", description="Test the alert pipeline with a mock error; does not change code.")
@owner_check()
async def simulate_crash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    mock_error = "Mock ValueError: simulated invalid transaction amount 'R149.99'. No real transaction or production service was affected."
    delivered = await send_alert("NexusAI Mock Incident", mock_error, 0xE67E22)
    await interaction.followup.send(
        "Mock incident created for alert-pipeline testing. " +
        ("Webhook alert delivered." if delivered else "Webhook alert was not delivered (check DISCORD_WEBHOOK_URL).") +
        " No production logs were intercepted and no source code was changed.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    log.error("Application command failed: %s", type(error).__name__)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("The command failed. Check the operator worker logs for details.", ephemeral=True)
        else:
            await interaction.response.send_message("The command failed. Check the operator worker logs for details.", ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not send application-command error response.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
