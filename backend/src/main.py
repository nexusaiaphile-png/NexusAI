import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

BASE_DIR = Path(__file__).resolve().parents[2]
PORTAL_DIR = BASE_DIR / "portal"
ROOT_INDEX = BASE_DIR / "index.html"

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


@app.get("/story.css", include_in_schema=False)
async def story_css():
    return FileResponse(PORTAL_DIR / "story.css", media_type="text/css")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return FileResponse(PORTAL_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    return FileResponse(PORTAL_DIR / "sitemap.xml", media_type="application/xml")


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

    EDGE_EVENTS.insert(0, event.model_dump())
    del EDGE_EVENTS[200:]

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
    site = EDGE_SITES.get(site_id)
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
    events = [item for item in EDGE_EVENTS if item.get("site_id") == site_id]
    return {"site_id": site_id, "events": events[:safe_limit]}


# Serve the client portal files (index.html, CSS and JavaScript) under /portal/.
# This must be mounted after the API/public routes so FastAPI does not let
# static-file routing swallow the health/API endpoints.
app.mount("/portal", StaticFiles(directory=PORTAL_DIR, html=True), name="portal")
