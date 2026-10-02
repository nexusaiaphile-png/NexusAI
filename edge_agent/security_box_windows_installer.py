"""NexusAI one-click Windows Security Box installer and service host."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

INSTALLER_TOKEN_PLACEHOLDER = "NEXUSAI_INSTALLER_TOKEN_PLACEHOLDER_" + "~" * 64
API_DEFAULT = "https://getnexusai.co.za"
LOG_DIR = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "NexusAI"
LOG_FILE = LOG_DIR / "security-box-installer.log"


def _log_setup() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _token_from_bundle() -> str:
    raw = INSTALLER_TOKEN_PLACEHOLDER
    prefix = "NEXUSAI_INSTALLER_TOKEN_PLACEHOLDER_"
    if not raw.startswith(prefix):
        return ""
    token = raw[len(prefix):].rstrip("~")
    if not token or token.startswith("PLACEHOLDER"):
        return ""
    return token


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin() -> None:
    params = " ".join('"' + arg.replace('"', '\\"') + '"' for arg in sys.argv[1:])
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    if result <= 32:
        raise RuntimeError(f"Windows elevation failed ({result}).")


def _service_command(*args: str) -> None:
    subprocess.run([sys.executable, *args], check=True)


def install() -> int:
    _log_setup()
    if not _is_admin():
        _relaunch_as_admin()
        return 0

    token = _token_from_bundle()
    if not token:
        raise RuntimeError("This NexusAI installer link is invalid or has expired.")

    from edge_agent import security_box

    logging.info("Starting NexusAI Security Box installation.")
    data = security_box.bootstrap(token, API_DEFAULT)
    site_id = str(data["site_id"])
    security_box.save_site_config(site_id, API_DEFAULT)
    logging.info("Security Box provisioned for %s.", site_id)

    _service_command("install")
    _service_command("start")

    subprocess.run(
        ["sc.exe", "config", "NexusAISecurityBox", "start=", "delayed-auto"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sc.exe", "failure", "NexusAISecurityBox", "reset=", "900",
         "actions=", "restart/60000/restart/120000/restart/300000"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    logging.info("NexusAI Security Box service installed and started.")
    return 0


def service_host() -> int:
    from edge_agent import security_box
    return security_box.run()


def main() -> int:
    _log_setup()
    args = [a.lower() for a in sys.argv[1:]]
    service_commands = {"install", "update", "remove", "start", "stop", "restart", "debug"}
    if any(a in service_commands for a in args):
        from edge_agent.windows_service import NexusAISecurityBoxService
        import win32serviceutil
        win32serviceutil.HandleCommandLine(NexusAISecurityBoxService)
        return 0

    if "-s" in args:
        return service_host()

    try:
        return install()
    except Exception:
        logging.exception("NexusAI Security Box installation failed.")
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                "NexusAI could not finish installing. Please open the installer again or contact NexusAI support.",
                "NexusAI Security Box",
                0x10,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
