"""Compatibility entry point for the Render deployment.

The FastAPI service is launched as backend.src.main, while older NexusAI
imports refer to hpp_integration as a top-level module. Re-export the real
Hik-Partner Pro integration so both import styles work.
"""

from backend.src.hpp_integration import *  # noqa: F401,F403
