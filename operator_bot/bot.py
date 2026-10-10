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
RENDER_API_BASE = "https://api.render.com/v1"

# Fail closed: never start a bot that is not explicitly configured.
missing = [
    name for name, value in (
        ("NEXUSAI_DISCORD_BOT_TOKEN", DISCORD_TOKEN),
        ("NEXUSAI_DISCORD_OWNER_ID", OWNER_ID_RAW),
        ("RENDER_API_KEY", RENDER_API_KEY),
        ("RENDER_SERVICE_ID", RENDER_SERVICE_ID),
    ) if not value
]
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("NEXUSAI_DISCORD_OWNER_ID must be a numeric Discord user ID.") from exc
if OWNER_ID <= 0:
    raise RuntimeError("NEXUSAI_DISCORD_OWNER_ID must be a positive Discord user ID.")

intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def owner_only(interaction: discord.Interaction) -> bool:
    return interaction.user is not None and interaction.user.id == OWNER_ID


async def render_get(path: str, params: dict[str, str] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{RENDER_API_BASE}{path}", headers=headers, params=params)
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


@bot.event
async def on_ready() -> None:
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s application commands; connected as %s", len(synced), bot.user)
    except Exception:
        log.exception("Could not sync Discord application commands")
    log.info("NexusAI Operator is online in SAFE MODE.")


@bot.tree.interaction_check
async def global_owner_gate(interaction: discord.Interaction) -> bool:
    if not owner_only(interaction):
        # Do not reveal command results or acknowledge non-owner command use.
        log.warning("Blocked command from non-owner Discord user ID %s", interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="status", description="Read the configured Render service status.")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}")
        service = data.get("service", data) if isinstance(data, dict) else {}
        name = service.get("name", "Unknown service")
        service_type = service.get("type", "Unknown")
        suspended = service.get("suspended")
        url = service.get("serviceDetails", {}).get("url") if isinstance(service.get("serviceDetails"), dict) else None
        status_text = "Suspended" if suspended is True else ("Not marked suspended" if suspended is False else "Not supplied by API")
        embed = discord.Embed(title="NexusAI Render Status", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Service", value=short(name, 200), inline=True)
        embed.add_field(name="Type", value=short(service_type, 80), inline=True)
        embed.add_field(name="Suspension", value=status_text, inline=True)
        embed.add_field(name="Service URL", value=short(url or "Not provided by Render API", 300), inline=False)
        embed.set_footer(text="Read-only check. Service metadata does not prove the app or database is healthy.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        log.warning("Render status request failed with HTTP %s", exc.response.status_code)
        await interaction.followup.send(f"Render API returned HTTP {exc.response.status_code}. Check the service ID and Render API key permissions in Render Environment.", ephemeral=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Render status request failed: %s", type(exc).__name__)
        await interaction.followup.send("Could not reach or parse the Render API response. Check the bot's Render logs; secret values are not shown here.", ephemeral=True)


@bot.tree.command(name="deploys", description="List recent deployments for the configured Render service.")
async def deploys(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}/deploys", {"limit": "5"})
        rows = data if isinstance(data, list) else data.get("deploys", []) if isinstance(data, dict) else []
        embed = discord.Embed(title="Recent NexusAI Deployments", color=discord.Color.blurple())
        if not rows:
            embed.description = "Render returned no deployment records in the expected response format."
        for item in rows[:5]:
            record = item.get("deploy", item) if isinstance(item, dict) else {}
            commit = record.get("commit", {})
            if not isinstance(commit, dict):
                commit = {}
            title = short(record.get("status", "Unknown status"), 100)
            detail = f"Created: {timestamp(record.get('createdAt'))}\nUpdated: {timestamp(record.get('updatedAt'))}\nCommit: {short(commit.get('id') or record.get('commitId') or 'Not provided', 100)}"
            embed.add_field(name=title, value=detail, inline=False)
        embed.set_footer(text="Read-only deployment history; this command does not deploy or roll back.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        log.warning("Render deployments request failed with HTTP %s", exc.response.status_code)
        await interaction.followup.send(f"Render API returned HTTP {exc.response.status_code}. Check service ID and API permissions.", ephemeral=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Render deployments request failed: %s", type(exc).__name__)
        await interaction.followup.send("Could not retrieve deployments. Check the bot's Render logs; secret values are not shown here.", ephemeral=True)


@bot.tree.command(name="diagnose", description="Run safe metadata checks; no simulated health metrics.")
async def diagnose(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    checks: list[tuple[str, str]] = []
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}")
        service = data.get("service", data) if isinstance(data, dict) else {}
        checks.append(("Render service metadata", "PASS — API returned service metadata" if service else "UNKNOWN — empty/unexpected response"))
        checks.append(("Service type", short(service.get("type", "Not provided"), 100)))
        checks.append(("Suspended flag", str(service.get("suspended", "Not provided"))))
    except httpx.HTTPStatusError as exc:
        checks.append(("Render service metadata", f"FAIL — HTTP {exc.response.status_code}"))
    except (httpx.HTTPError, ValueError) as exc:
        checks.append(("Render service metadata", f"FAIL — {type(exc).__name__}"))

    embed = discord.Embed(title="NexusAI Safe Diagnostics", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    for label, result in checks:
        embed.add_field(name=label, value=short(result, 500), inline=False)
    embed.add_field(
        name="Not checked yet",
        value="Application runtime logs, database connectivity, POS intake, HPP webhook delivery, camera events and response latency. No metrics are fabricated.",
        inline=False,
    )
    embed.set_footer(text="Next move: verify the service ID and add read-only log retrieval after API access is confirmed.")
    await interaction.followup.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
