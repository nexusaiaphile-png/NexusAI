import os
import time
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

BASE_DIR = Path(__file__).resolve().parents[2]
PORTAL_DIR = BASE_DIR / "portal"
ROOT_INDEX = BASE_DIR / "index.html"
MOBILE_DIR = BASE_DIR / "mobile"

# In-memory edge state for the current NexusAI service instance.
# Production persistence can be moved to Postgres without changing the API contract.
EDGE_SITES: dict[str, dict] = {}
EDGE_EVENTS: list[dict] = []
DB_PATH = Path(os.getenv("NEXUSAI_DB_PATH", str(BASE_DIR / "nexusai.db")))
DB_LOCK = __import__("threading").Lock()

app = FastAPI(
    title="NexusAI API",
    description="NexusAI Security Client Portal API",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CameraVerificationRequest(BaseModel):
    camera_name: str = Field(default="Camera 1", max_length=100)
    camera_ip: str = Field(..., max_length=255)
    camera_port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(..., max_length=100)
    password: SecretStr
    location: str = Field(default="Main Entrance", max_length=200)


class EdgeCamera(BaseModel):
    camera_id: str
    camera_name: str
    location: str
    verified: bool = False


class EdgeHeartbeat(BaseModel):
    site_id: str
    agent_version: str
    timestamp: str
    status: str
    cameras: list[EdgeCamera] = Field(default_factory=list)


class EdgeVerification(BaseModel):
    site_id: str
    camera_id: str
    camera_name: str
    location: str
    network: str
    camera: str
    credentials: str
    nexusai: str
    verified: bool = False
    error: str | None = None
    device_type: str | None = None
    channels: list[dict] = Field(default_factory=list)


class EdgeEvent(BaseModel):
    site_id: str
    camera_id: str
    camera_name: str
    location: str
    event: str
    severity: str
    timestamp: str
    source: str
    snapshot_available: bool = False


def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS sites (
        site_id TEXT PRIMARY KEY, status TEXT, agent_version TEXT, timestamp TEXT,
        received_at REAL, cameras_json TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT, camera_id TEXT,
        camera_name TEXT, location TEXT, event TEXT, severity TEXT, timestamp TEXT,
        source TEXT, snapshot_available INTEGER
    )""")
    conn.commit()
    return conn

def persist_site(site):
    import json
    with DB_LOCK:
        conn=db_conn()
        conn.execute("""INSERT INTO sites(site_id,status,agent_version,timestamp,received_at,cameras_json)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET
                       status=excluded.status,agent_version=excluded.agent_version,timestamp=excluded.timestamp,
                       received_at=excluded.received_at,cameras_json=excluded.cameras_json""",
                     (site["site_id"],site.get("status"),site.get("agent_version"),site.get("timestamp"),
                      site.get("received_at",time.time()),json.dumps(site.get("cameras",[]))))
        conn.commit(); conn.close()

def persist_event(event):
    with DB_LOCK:
        conn=db_conn()
        conn.execute("""INSERT INTO events(site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                     (event["site_id"],event["camera_id"],event["camera_name"],event["location"],event["event"],
                      event["severity"],event["timestamp"],event["source"],int(bool(event.get("snapshot_available")))))
        conn.commit(); conn.close()

def load_site(site_id):
    import json
    with DB_LOCK:
        conn=db_conn(); row=conn.execute("SELECT * FROM sites WHERE site_id=?",(site_id,)).fetchone(); conn.close()
    if not row: return None
    return {"site_id":row["site_id"],"status":row["status"],"agent_version":row["agent_version"],
            "timestamp":row["timestamp"],"received_at":row["received_at"],"cameras":json.loads(row["cameras_json"] or "[]")}

def load_events(site_id,limit):
    with DB_LOCK:
        conn=db_conn(); rows=conn.execute("SELECT site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available FROM events WHERE site_id=? ORDER BY id DESC LIMIT ?",(site_id,limit)).fetchall(); conn.close()
    return [dict(r, snapshot_available=bool(r["snapshot_available"])) for r in rows]

def dispatch_alert(event):
    webhook = os.getenv("NEXUSAI_ALERT_WEBHOOK_URL", "").strip()
    if not webhook: return
    import json, urllib.request
    try:
        req=urllib.request.Request(webhook, data=json.dumps({"type":"nexusai_security_event","event":event}).encode(), headers={"Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as exc:
        import logging; logging.warning("Alert webhook failed: %s", exc)

def send_whatsapp_alert(event):
    phone_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID","").strip()
    token=os.getenv("WHATSAPP_ACCESS_TOKEN","").strip()
    recipient=os.getenv("WHATSAPP_ALERT_RECIPIENT","").strip()
    if not (phone_id and token and recipient): return False
    import json, urllib.request
    text=f"NexusAI SECURITY ALERT\\n{event['event']}\\nCamera: {event['camera_name']}\\nLocation: {event['location']}\\nSeverity: {event['severity']}\\nTime: {event['timestamp']}"
    url=f"https://graph.facebook.com/v23.0/{phone_id}/messages"
    payload={"messaging_product":"whatsapp","to":recipient,"type":"text","text":{"body":text}}
    try:
        req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=10).read(); return True
    except Exception as exc:
        import logging; logging.warning("WhatsApp alert failed: %s", exc); return False

def require_edge_token(token: str | None):
    expected = os.getenv("NEXUSAI_EDGE_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid NexusAI Edge Agent token")


@app.get("/", include_in_schema=False)
async def public_home():
    return FileResponse(ROOT_INDEX)


@app.get("/about", include_in_schema=False)
async def public_about():
    return FileResponse(PORTAL_DIR / "about.html")


@app.get("/founder", include_in_schema=False)
async def public_founder():
    return FileResponse(PORTAL_DIR / "founder.html")


@app.get("/portal", include_in_schema=False)
@app.get("/portal/", include_in_schema=False)
async def client_portal():
    return FileResponse(PORTAL_DIR / "index.html")

@app.get("/mobile", include_in_schema=False)
@app.get("/mobile/", include_in_schema=False)
async def mobile_portal():
    return FileResponse(MOBILE_DIR / "index.html")

@app.get("/mobile/app.js", include_in_schema=False)
async def mobile_app_js():
    return FileResponse(MOBILE_DIR / "app.js", media_type="application/javascript")

@app.get("/mobile/style.css", include_in_schema=False)
async def mobile_style_css():
    return FileResponse(MOBILE_DIR / "style.css", media_type="text/css")


@app.get("/story.css", include_in_schema=False)
async def story_css():
    return FileResponse(PORTAL_DIR / "story.css", media_type="text/css")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse(PORTAL_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    return FileResponse(PORTAL_DIR / "sitemap.xml", media_type="application/xml")


@app.get("/downloads/install_mac.sh", include_in_schema=False)
async def download_mac_installer():
    return FileResponse(
        BASE_DIR / "edge_agent" / "install_mac.sh",
        media_type="application/x-sh",
        filename="NexusAI-Edge-Agent-macOS.sh",
    )


@app.get("/downloads/install_windows.ps1", include_in_schema=False)
async def download_windows_installer():
    return FileResponse(
        BASE_DIR / "edge_agent" / "install_windows.ps1",
        media_type="text/plain",
        filename="NexusAI-Edge-Agent-Windows.ps1",
    )


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "NexusAI Backend", "version": "2.0.0"}


@app.get("/api/status")
async def api_status():
    return {
        "nexusai": "ONLINE",
        "security_engine": "READY",
        "camera_verification": "EDGE_AGENT",
        "edge_agent": "READY",
        "api_version": "2.0.0",
    }


@app.post("/api/cameras/verify")
async def verify_camera(request: CameraVerificationRequest):
    return {
        "verified": False,
        "network": "EDGE_AGENT_REQUIRED",
        "camera": "WAITING",
        "credentials": "NOT_CHECKED",
        "nexusai": "READY",
        "camera_name": request.camera_name,
        "location": request.location,
        "verification_mode": "edge",
        "error": "NexusAI Edge Agent is required to verify a private customer camera.",
    }


@app.post("/api/edge/heartbeat")
async def edge_heartbeat(
    heartbeat: EdgeHeartbeat,
    x_nexusai_edge_token: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token)

    EDGE_SITES[heartbeat.site_id] = {
        "site_id": heartbeat.site_id,
        "status": heartbeat.status,
        "agent_version": heartbeat.agent_version,
        "timestamp": heartbeat.timestamp,
        "received_at": time.time(),
        "cameras": [camera.model_dump() for camera in heartbeat.cameras],
    }

    persist_site(EDGE_SITES[heartbeat.site_id])

    return {
        "accepted": True,
        "service": "NexusAI Edge Agent",
        "site_id": heartbeat.site_id,
        "status": "ONLINE",
        "received_at": heartbeat.timestamp,
    }


@app.post("/api/edge/verify")
async def edge_verify(
    verification: EdgeVerification,
    x_nexusai_edge_token: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token)
    return {
        "accepted": True,
        "verified": verification.verified,
        "site_id": verification.site_id,
        "camera_id": verification.camera_id,
        "camera_name": verification.camera_name,
        "network": verification.network,
        "camera": verification.camera,
        "credentials": verification.credentials,
        "nexusai": "CONNECTED" if verification.verified else "NOT_CONNECTED",
        "error": verification.error,
        "channels": verification.channels,
    }


@app.post("/api/edge/events")
async def edge_event(
    event: EdgeEvent,
    x_nexusai_edge_token: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token)

    event_data = event.model_dump()    EDGE_EVENTS.insert(0, event_data)\n    del EDGE_EVENTS[200:]\n    dispatch_alert(event_data)
    site = EDGE_SITES.setdefault(event.site_id, {
        "site_id": event.site_id,
        "status": "ONLINE",
        "received_at": time.time(),
        "cameras": [],
    })
    site["received_at"] = time.time()

    return {
        "accepted": True,
        "site_id": event.site_id,
        "camera_id": event.camera_id,
        "event": event.event,
        "severity": event.severity,
        "timestamp": event.timestamp,
    }


@app.get("/api/portal/status")
async def portal_status(site_id: str = "site-demo"):
    site = EDGE_SITES.get(site_id) or load_site(site_id)
    if not site:
        return {"site_id": site_id, "edge_agent": "OFFLINE", "status": "WAITING", "cameras": []}

    age = time.time() - float(site.get("received_at", 0))
    return {
        "site_id": site_id,
        "edge_agent": "ONLINE" if age <= 90 else "OFFLINE",
        "status": site.get("status", "UNKNOWN"),
        "agent_version": site.get("agent_version"),
        "cameras": site.get("cameras", []),
    }


@app.get("/api/portal/events")
async def portal_events(site_id: str = "site-demo", limit: int = 50):
    safe_limit = max(1, min(limit, 100))
    events = load_events(site_id, safe_limit)
    return {"site_id": site_id, "events": events}


# Serve the client portal files (index.html, CSS and JavaScript) under /portal/.
# This must be mounted after the API/public routes so FastAPI does not let
# static-file routing swallow the health/API endpoints.
app.mount("/portal", StaticFiles(directory=PORTAL_DIR, html=True), name="portal")
