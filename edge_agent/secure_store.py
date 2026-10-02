"""OS-native secure storage for NexusAI Security Box.

Windows uses machine-scoped DPAPI so the LocalSystem Windows service can
retrieve the secrets after reboot. macOS uses the system Keychain because the
Security Box runs as a LaunchDaemon/root service.
"""
from __future__ import annotations
import base64
import json
import os
import subprocess
from pathlib import Path

SERVICE = "NexusAI Security Box"
DATA_DIR = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "NexusAI" if os.name == "nt" else Path("/Library/Application Support/NexusAI")
VAULT_PATH = DATA_DIR / "secure-vault.json"

def _load_vault() -> dict:
    try:
        return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_vault(vault: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(vault), encoding="utf-8")
    if os.name == "nt":
        try:
            import win32security
            sd = win32security.GetFileSecurity(str(VAULT_PATH), win32security.DACL_SECURITY_INFORMATION) if VAULT_PATH.exists() else None
            os.chmod(tmp, 0o600)
        except Exception:
            os.chmod(tmp, 0o600)
    else:
        os.chmod(tmp, 0o600)
    tmp.replace(VAULT_PATH)

def _windows_set(name: str, value: str) -> None:
    import win32crypt
    blob = win32crypt.CryptProtectData(
        value.encode("utf-8"), SERVICE, None, None, None,
        win32crypt.CRYPTPROTECT_LOCAL_MACHINE,
    )[1]
    vault = _load_vault()
    vault[name] = base64.b64encode(blob).decode("ascii")
    _save_vault(vault)

def _windows_get(name: str) -> str:
    import win32crypt
    value = _load_vault().get(name)
    if not value:
        return ""
    try:
        raw = base64.b64decode(value)
        return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""

def _mac_set(name: str, value: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["security", "add-generic-password", "-a", name, "-s", SERVICE, "-w", value, "-U", "/Library/Keychains/System.keychain"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def _mac_get(name: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w", "/Library/Keychains/System.keychain"],
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""

def set_secret(name: str, value: str) -> None:
    if os.name == "nt":
        _windows_set(name, value)
    elif sys_platform() == "darwin":
        _mac_set(name, value)
    else:
        raise RuntimeError("NexusAI Security Box secure storage supports Windows and macOS only.")

def get_secret(name: str) -> str:
    if os.name == "nt":
        return _windows_get(name)
    if sys_platform() == "darwin":
        return _mac_get(name)
    return ""

def sys_platform() -> str:
    import sys
    return sys.platform

def set_site_credentials(site_id: str, edge_token: str) -> None:
    set_secret(f"{site_id}:edge_token", edge_token)

def get_site_token(site_id: str) -> str:
    return get_secret(f"{site_id}:edge_token")

def set_hikvision_credentials(site_id: str, username: str, password: str) -> None:
    set_secret(f"{site_id}:hikvision_username", username)
    set_secret(f"{site_id}:hikvision_password", password)

def get_hikvision_credentials(site_id: str) -> tuple[str, str]:
    return (
        get_secret(f"{site_id}:hikvision_username"),
        get_secret(f"{site_id}:hikvision_password"),
    )
