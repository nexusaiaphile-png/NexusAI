"""NexusAI one-click Windows Security Box installer and service host."""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import re
from pathlib import Path

API_DEFAULT = "https://getnexusai.co.za"
LOG_DIR = Path(os.getenv("PROGRAMDATA", r"C:\\ProgramData")) / "NexusAI"
LOG_FILE = LOG_DIR / "security-box-installer.log"
