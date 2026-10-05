"""NexusAI encrypted command payloads."""
import os, json, base64, hashlib
from cryptography.fernet import Fernet, InvalidToken

def _key():
    root=os.getenv("NEXUSAI_APP_LINK_SECRET","").strip()
    if not root: raise RuntimeError("NEXUSAI_APP_LINK_SECRET is not configured")
    key=base64.urlsafe_b64encode(hashlib.sha256(("nexusai-credential-vault:"+root).encode()).digest())
    return Fernet(key)
def seal(payload):
    return _key().encrypt(json.dumps(payload,separators=(",",":")).encode()).decode()
def open_sealed(value):
    try: return json.loads(_key().decrypt(value.encode()).decode())
    except (InvalidToken,ValueError,TypeError,json.JSONDecodeError) as exc:
        raise RuntimeError("NexusAI encrypted command could not be opened") from exc
