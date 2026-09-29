"""Compatibility entry point for the NexusAI Cloud service.

The production Render service starts backend.src.main directly.
This file intentionally contains no legacy WhatsApp/email alert code.
"""

from backend.src.main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.src.main:app", host="0.0.0.0", port=8000, reload=False)
