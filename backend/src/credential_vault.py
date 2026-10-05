"""NexusAI encrypted command payloads."""
import os, json
from cryptography.fernet import Fernet, InvalidToken

def _key():
    value=os.getenv("NEXUSAI_CREDENTIAL_ENCRYPTION_KEY","").strip()
    if not value:
        raise RuntimeError("NEXUSAI_CREDENTIAL_ENCRYPTION_KEY is not configured")
    return Fernet(value.encode())
def seal(payload):
    return _key().encrypt(json.dumps(payload,separators=(",",":")).encode()).decode()
def open_sealed(value):
    try: return json.loads(_key().decrypt(value.encode()).decode())
    except (InvalidToken,ValueError,TypeError,json.JSONDecodeError) as exc:
        raise RuntimeError("NexusAI encrypted command could not be opened") from exc
