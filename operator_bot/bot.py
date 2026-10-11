"""Owner-only, read-only NexusAI DevOps Discord bot.

This bot reads Render/GitHub metadata and can send mock/test Discord alerts.
It never edits source code, deploys services, or changes production settings.
"""
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
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


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP health endpoint for Render Web Service deployments."""

    def do_GET(self) -> None:
        if self.path not in ("/", "/health", "/healthz"):
            self.send_error(404)
            return
        body = b'{"status":"ok","service":"nexusai-operator"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid noisy access logs; startup and bot errors go through logging.
        return


def start_health_server() -> None:
    """Bind a tiny health endpoint to Render's assigned port."""
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    log.info("Health endpoint listening on 0.0.0.0:%s", port)


def owner_check():
    """Restrict every slash command to the configured owner."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == OWNER_ID:
            return True
        log.warning("Denied operator command for Discord user ID %s",
                    getattr(interaction.user, "id", "unknown"))
        if not interaction.response.is_done():
            await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False
    return app_commands.check(predicate)


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


async def render_get(path: str, params: dict[str, str] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{RENDER_API_BASE}{path}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()


async def github_get(path: str, params: dict[str, str] | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        response = await client.get(f"{GITHUB_API_BASE}{path}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()


async def send_alert(title: str, description: str, color: int = 0x3498DB) -> bool:
    """Send an optional Discord webhook alert."""
    if not DISCORD_WEBHOOK_URL:
        log.info("Alert not sent because DISCORD_WEBHOOK_URL is not configured.")
        return False
    parsed_url = urlsplit(DISCORD_WEBHOOK_URL)
    if parsed_url.scheme != "https" or not parsed_url.netloc or not parsed_url.path.startswith("/api/webhooks/"):
        log.warning("Discord webhook URL is invalid; expected a full https://discord.com/api/webhooks/... URL.")
        return False
    payload = {"embeds": [{
        "title": short(title, 240),
        "description": short(description, 3500),
        "color": color,
        "footer": {"text": "NexusAI Operator • Read-only mode"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}
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
    log.info("NexusAI Operator online in read-only mode.")
    await send_alert(
        "🚀 Autonomous Core Initialized",
        "NexusAI Operator bot is online on Render. Monitoring commands are ready; "
        "no production code or service settings are modified by this bot.",
        0x2ECC71,
    )


@bot.tree.command(name="status", description="Read the configured Render service status.")
@owner_check()
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}")
        service = data.get("service", data) if isinstance(data, dict) else {}
        name = service.get("name", "Configured service")
        service_type = service.get("type", "Unknown")
        suspended = service.get("suspended", "Unknown")
        embed = discord.Embed(title="NexusAI Render Status", color=discord.Color.blue(),
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Service", value=short(name, 200), inline=True)
        embed.add_field(name="Type", value=short(service_type, 100), inline=True)
        embed.add_field(name="Suspended", value=short(suspended, 100), inline=True)
        embed.set_footer(text=f"Service ID: {RENDER_SERVICE_ID}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"Render API returned HTTP {exc.response.status_code}. Check the API key permissions and service ID.",
            ephemeral=True,
        )
    except httpx.HTTPError as exc:
        log.exception("Render status request failed")
        await interaction.followup.send(f"Could not reach Render API ({type(exc).__name__}).", ephemeral=True)
    except Exception:
        log.exception("Status command failed")
        await interaction.followup.send("Status check failed. Review the worker logs.", ephemeral=True)


@bot.tree.command(name="deploys", description="Show recent deployment records for the configured Render service.")
@owner_check()
async def deploys(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await render_get(f"/services/{RENDER_SERVICE_ID}/deploys", {"limit": "5"})
        records = data if isinstance(data, list) else data.get("deploys", data.get("data", []))
        if not records:
            await interaction.followup.send("Render returned no deployment records.", ephemeral=True)
            return
        lines = []
        for item in records[:5]:
            deploy = item.get("deploy", item) if isinstance(item, dict) else {}
            lines.append(
                f"• **{short(deploy.get('status', 'Unknown'), 60)}** — "
                f"{timestamp(deploy.get('createdAt') or deploy.get('created_at'))}\n"
                f"  ID: `{short(deploy.get('id', 'Unknown'), 80)}`"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"Render API returned HTTP {exc.response.status_code}. Check API key permissions and service ID.",
            ephemeral=True,
        )
    except Exception:
        log.exception("Deploy history request failed")
        await interaction.followup.send("Could not read deployment history. Review the worker logs.", ephemeral=True)


@bot.tree.command(name="diagnose", description="Run safe API connectivity checks.")
@owner_check()
async def diagnose(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = []
    try:
        await render_get(f"/services/{RENDER_SERVICE_ID}")
        lines.append("✅ Render API: service metadata request succeeded")
    except httpx.HTTPStatusError as exc:
        lines.append(f"❌ Render API: HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        lines.append(f"❌ Render API: connection error ({type(exc).__name__})")
    if GITHUB_TOKEN:
        try:
            await github_get(f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
            lines.append("✅ GitHub API: repository metadata request succeeded")
        except httpx.HTTPStatusError as exc:
            lines.append(f"❌ GitHub API: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            lines.append(f"❌ GitHub API: connection error ({type(exc).__name__})")
    else:
        lines.append("ℹ️ GitHub API: skipped (GITHUB_TOKEN is not configured)")
    lines.append("ℹ️ Checks cover API connectivity only, not HPP, POS, camera, or database health.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="github", description="Read repository metadata and recent commits on the operator branch.")
@owner_check()
async def github(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        repo = await github_get(f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}")
        commits = await github_get(
            f"/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/commits",
            {"sha": GITHUB_BRANCH, "per_page": "5"},
        )
        lines = [
            f"**Repository:** {repo.get('full_name', 'Unknown')}",
            f"**Default branch:** {repo.get('default_branch', 'Unknown')}",
            f"**Configured branch:** {GITHUB_BRANCH}",
            "**Recent commits:**",
        ]
        for item in commits[:5] if isinstance(commits, list) else []:
            commit = item.get("commit", {})
            message = (commit.get("message", "No message").splitlines() or ["No message"])[0]
            lines.append(f"• `{short(item.get('sha', 'unknown'), 10)}` {short(message, 140)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except httpx.HTTPStatusError as exc:
        await interaction.followup.send(
            f"GitHub API returned HTTP {exc.response.status_code}. Check repository access and GITHUB_TOKEN.",
            ephemeral=True,
        )
    except Exception:
        log.exception("GitHub command failed")
        await interaction.followup.send("Could not read GitHub metadata. Review the worker logs.", ephemeral=True)


@bot.tree.command(name="test_alert", description="Send a test alert to the configured Discord webhook.")
@owner_check()
async def test_alert(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    sent = await send_alert("NexusAI Operator Test Alert", "This is a manual test of the optional alert webhook.", 0x3498DB)
    message = "Test alert sent." if sent else "Alert was not sent. Configure DISCORD_WEBHOOK_URL and check worker logs."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="simulate_crash", description="Send a mock incident alert; does not crash or modify anything.")
@owner_check()
async def simulate_crash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    sent = await send_alert(
        "🧪 Simulated NexusAI Incident",
        "Mock incident only: this command does not crash a service, read production transactions, "
        "change source code, or trigger a deployment.",
        0xE67E22,
    )
    message = "Mock incident alert sent." if sent else "Mock incident was prepared, but no webhook alert was sent. Configure DISCORD_WEBHOOK_URL."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    log.error("Application command failed: %s", type(error).__name__)
    message = "Command failed. Review the worker logs."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    start_health_server()
    bot.run(DISCORD_TOKEN, log_handler=None)
