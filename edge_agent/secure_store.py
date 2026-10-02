"""OS-backed secure storage for NexusAI Security Box secrets."""
from __future__ import annotations
import keyring

SERVICE = "NexusAI Security Box"

def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)

def get_secret(name: str) -> str:
    return keyring.get_password(SERVICE, name) or ""

def set_site_credentials(site_id: str, edge_token: str) -> None:
    set_secret(f"{site_id}:edge_token", edge_token)

def set_hikvision_credentials(site_id: str, username: str, password: str) -> None:
    set_secret(f"{site_id}:hikvision_username", username)
    set_secret(f"{site_id}:hikvision_password", password)

def get_site_token(site_id: str) -> str:
    return get_secret(f"{site_id}:edge_token")

def get_hikvision_credentials(site_id: str) -> tuple[str, str]:
    return (
        get_secret(f"{site_id}:hikvision_username"),
        get_secret(f"{site_id}:hikvision_password"),
    )
