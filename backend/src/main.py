import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, SecretStr
from workers import asgi


app = FastAPI(
    title="NexusAI API",
    description="NexusAI Security Client Portal API",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://getnexusai.co.za",
        "https://www.getnexusai.co.za",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
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
    cameras: list[EdgeCamera] = []


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


@app.get("/")
async def root():
    return {
        "service": "NexusAI API",
        "status": "ONLINE",
        "version": "1.1.0",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "NexusAI Backend",
    }


@app.get("/api/status")
async def api_status():
    return {
        "nexusai": "ONLINE",
        "security_engine": "READY",
        "camera_verification": "EDGE_AGENT",
        "edge_agent": "READY",
        "api_version": "1.1.0",
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
    }


@app.post("/api/edge/events")
async def edge_event(
    event: EdgeEvent,
    x_nexusai_edge_token: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token)
    return {
        "accepted": True,
        "site_id": event.site_id,
        "camera_id": event.camera_id,
        "event": event.event,
        "severity": event.severity,
        "timestamp": event.timestamp,
    }


Default = asgi.entrypoint(app)
