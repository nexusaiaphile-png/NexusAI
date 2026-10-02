"""NexusAI Security Box bootstrap and supervisor."""
from __future__ import annotations
import argparse
import os
import requests
import json
from pathlib import Path
from edge_agent import secure_store

API_DEFAULT = "https://getnexusai.co.za"
DATA_DIR = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "NexusAI" if os.name == "nt" else Path("/Library/Application Support/NexusAI")
CONFIG_PATH = DATA_DIR / "security-box.json"

def bootstrap(token: str, api_url: str) -> dict:
    response = requests.post(
        f"{api_url.rstrip('/')}/api/edge/bootstrap",
        json={"installer_token": token},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    site_id = str(data["site_id"])
    secure_store.set_site_credentials(site_id, str(data["edge_token"]))
    return data

def save_site_config(site_id: str, api_url: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"site_id": site_id, "api_url": api_url.rstrip("/")}), encoding="utf-8")
    tmp.replace(CONFIG_PATH)

def load_site_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def configure(site_id: str, api_url: str) -> None:
    os.environ["NEXUSAI_SITE_ID"] = site_id
    os.environ["NEXUSAI_API_URL"] = api_url.rstrip("/")
    os.environ["SITE_CONFIG_PATH"] = str(CONFIG_PATH)
    token = secure_store.get_site_token(site_id)
    if not token:
        raise RuntimeError("NexusAI site credential is missing from the OS secure store.")
    os.environ["NEXUSAI_EDGE_TOKEN"] = token
    username, password = secure_store.get_hikvision_credentials(site_id)
    if username:
        os.environ["CAM_USER"] = username
    if password:
        os.environ["CAM_PASS"] = password

def run() -> int:
    parser = argparse.ArgumentParser(description="NexusAI Security Box")
    parser.add_argument("--installer-token", default=os.getenv("NEXUSAI_INSTALLER_TOKEN", ""))
    parser.add_argument("--site-id", default=os.getenv("NEXUSAI_SITE_ID", ""))
    parser.add_argument("--api-url", default=os.getenv("NEXUSAI_API_URL", API_DEFAULT))
    parser.add_argument("--hikvision-username")
    parser.add_argument("--hikvision-password")
    args = parser.parse_args()

    if args.installer_token:
        data = bootstrap(args.installer_token.strip(), args.api_url)
        args.site_id = str(data["site_id"])

    if not args.site_id:
        saved = load_site_config()
        args.site_id = str(saved.get("site_id") or "")
        args.api_url = str(saved.get("api_url") or args.api_url)
    if not args.site_id:
        raise SystemExit("NexusAI Security Box is not provisioned.")

    if args.installer_token:
        save_site_config(args.site_id, args.api_url)

    if args.hikvision_username is not None and args.hikvision_password is not None:
        secure_store.set_hikvision_credentials(
            args.site_id, args.hikvision_username, args.hikvision_password
        )

    configure(args.site_id, args.api_url)

    from edge_agent import agent
    agent.SITE_ID = args.site_id
    agent.EDGE_AGENT_TOKEN = os.environ["NEXUSAI_EDGE_TOKEN"]
    agent.API_BASE_URL = args.api_url.rstrip("/")
    agent.UPDATE_BASE_URL = agent.API_BASE_URL
    return agent.main()

if __name__ == "__main__":
    raise SystemExit(run())
