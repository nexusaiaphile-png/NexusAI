"""One-click Windows installer and always-on service for NexusAI Security Box."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from edge_agent import security_box

API_DEFAULT = "https://getnexusai.co.za"
SERVICE_NAME = "NexusAISecurityBox"
SERVICE_DISPLAY = "NexusAI Security Box"
SERVICE_DESCRIPTION = "NexusAI always-on local CCTV security service."
LOG_DIR = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "NexusAI"
LOG_FILE = LOG_DIR / "security-box-installer.log"


class NexusAISecurityBoxService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("NexusAI Security Box started.")
        security_box.run()


def _installer_token() -> str:
    """Read the short-lived site token from the downloaded EXE filename."""
    name = Path(sys.executable).name if getattr(sys, "frozen", False) else Path(sys.argv[0]).name
    match = re.search(r"NexusAI-SecurityBox-([A-Za-z0-9_-]{40,220})\.exe$", name, re.IGNORECASE)
    if not match:
        raise RuntimeError("This NexusAI installer link is missing or has expired. Please scan the site QR code again.")
    return match.group(1)


def _log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s")
        logging.info(message)
    except Exception:
        pass


def install_service() -> None:
    try:
        win32serviceutil.InstallService(
            NexusAISecurityBoxService,
            SERVICE_NAME,
            SERVICE_DISPLAY,
            startType=win32service.SERVICE_AUTO_START,
            description=SERVICE_DESCRIPTION,
            exeName=sys.executable,
            delayedstart=True,
        )
    except win32service.error as exc:
        # Re-running the site installer should repair/update the existing service,
        # not fail because the service already exists.
        if getattr(exc, "winerror", None) == 1073:
            win32serviceutil.ChangeServiceConfig(
                NexusAISecurityBoxService,
                SERVICE_NAME,
                startType=win32service.SERVICE_AUTO_START,
                exeName=sys.executable,
                description=SERVICE_DESCRIPTION,
                displayName=SERVICE_DISPLAY,
                delayedstart=True,
            )
        else:
            raise


def provision_and_start() -> int:
    _log("NexusAI Security Box installer started.")
    token = _installer_token()
    data = security_box.bootstrap(token, API_DEFAULT)
    site_id = str(data["site_id"])
    security_box.save_site_config(site_id, API_DEFAULT)
    install_service()
    try:
        win32serviceutil.StartService(SERVICE_NAME)
    except Exception as exc:
        # A service that is already running is a successful outcome.
        text = str(exc).lower()
        if "already" not in text and "1056" not in text:
            raise
    _log("NexusAI Security Box installed and running for site %s.",)
    return 0


def main() -> int:
    if "--installer-token" in sys.argv:
        # Kept for enterprise/RMM deployments where the token is supplied
        # explicitly instead of being encoded in the downloaded filename.
        index = sys.argv.index("--installer-token")
        if index + 1 >= len(sys.argv):
            raise SystemExit("NexusAI installer token is missing.")
        token = sys.argv[index + 1].strip()
        data = security_box.bootstrap(token, API_DEFAULT)
        security_box.save_site_config(str(data["site_id"]), API_DEFAULT)
        install_service()
        win32serviceutil.StartService(SERVICE_NAME)
        return 0

    # When Windows Service Control Manager launches this EXE without arguments,
    # pywin32 enters the service dispatcher and calls SvcDoRun.
    win32serviceutil.HandleCommandLine(NexusAISecurityBoxService)
    return 0


if __name__ == "__main__":
    if "--installer-token" in sys.argv or (
        getattr(sys, "frozen", False)
        and re.search(r"NexusAI-SecurityBox-[A-Za-z0-9_-]{40,220}\.exe$", Path(sys.executable).name, re.I)
        and len(sys.argv) == 1
    ):
        try:
            raise SystemExit(provision_and_start())
        except Exception as exc:
            _log("Installation failed: %s", exc)
            raise
    raise SystemExit(main())
