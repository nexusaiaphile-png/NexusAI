from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, SecretStr
from workers import asgi


# ============================================================
# NEXUSAI API
# Cloudflare Python Worker + FastAPI
# ============================================================

app = FastAPI(
    title="NexusAI API",
    description="NexusAI Security Client Portal API",
    version="1.0.0",
)


# ============================================================
# CORS
# ============================================================

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


# ============================================================
# CAMERA REQUEST
# ============================================================

class CameraVerificationRequest(BaseModel):
    camera_name: str = Field(
        default="Camera 1",
        max_length=100,
    )

    camera_ip: str = Field(
        ...,
        max_length=255,
    )

    camera_port: int = Field(
        default=80,
        ge=1,
        le=65535,
    )

    username: str = Field(
        ...,
        max_length=100,
    )

    password: SecretStr

    location: str = Field(
        default="Main Entrance",
        max_length=200,
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "NexusAI API",
        "status": "ONLINE",
        "version": "1.0.0",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "NexusAI Backend",
    }


# ============================================================
# API STATUS
# ============================================================

@app.get("/api/status")
async def api_status():
    return {
        "nexusai": "ONLINE",
        "security_engine": "READY",
        "camera_verification": "READY",
        "api_version": "1.0.0",
    }


# ============================================================
# CAMERA VERIFICATION
# ============================================================

@app.post("/api/cameras/verify")
async def verify_camera(
    request: CameraVerificationRequest,
):
    """
    Initial NexusAI verification endpoint.

    The public Cloudflare API does NOT attempt to connect
    directly to a customer's private LAN camera.

    The production architecture will use the NexusAI
    Edge Agent at the customer's premises to perform
    the actual camera verification.
    """

    return {
        "verified": False,
        "network": "EDGE_AGENT_REQUIRED",
        "camera": "WAITING",
        "credentials": "NOT_CHECKED",
        "nexusai": "READY",
        "camera_name": request.camera_name,
        "location": request.location,
        "verification_mode": "edge",
        "error": (
            "NexusAI Edge Agent is required to verify "
            "a private customer camera."
        ),
    }


# ============================================================
# CLOUDFLARE ASGI ENTRYPOINT
# ============================================================

Default = asgi.entrypoint(app)
