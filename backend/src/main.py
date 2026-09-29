import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

BASE_DIR = Path(__file__).resolve().parents[2]
PORTAL_DIR = BASE_DIR / "portal"
ROOT_INDEX = BASE_DIR / "index.html"
APP_DIR = BASE_DIR / "nexusai-app"
DB_PATH = Path(os.getenv("NEXUSAI_DB_PATH", str(BASE_DIR / "nexusai.db")))
DB_LOCK = threading.RLock()
PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nexusai-push")

EDGE_SITES: dict[str, dict] = {}
EDGE_EVENTS: list[dict] = []

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(
    title="NexusAI Security Cloud",
    description="NexusAI Hikvision security cloud and native mobile push notification service.",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://getnexusai.co.za", "https://www.getnexusai.co.za"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


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


class PushSubscriptionRequest(BaseModel):
    site_id: str = Field(..., min_length=3, max_length=100)
    subscription: dict
    install_token: str = Field(..., min_length=20, max_length=500)


class PushTestRequest(BaseModel):
    site_id: str = Field(..., min_length=3, max_length=100)
    install_token: str = Field(..., min_length=20, max_length=500)


def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _sqlite():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    if database_url():
        import psycopg
        with psycopg.connect(database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_sites (
                        site_id TEXT PRIMARY KEY,
                        status TEXT,
                        agent_version TEXT,
                        timestamp TEXT,
                        received_at DOUBLE PRECISION,
                        cameras_json TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_events (
                        id BIGSERIAL PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        camera_id TEXT,
                        camera_name TEXT,
                        location TEXT,
                        event TEXT,
                        severity TEXT,
                        timestamp TEXT,
                        source TEXT,
                        snapshot_available BOOLEAN
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_push_subscriptions (
                        id BIGSERIAL PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        endpoint TEXT NOT NULL UNIQUE,
                        subscription_json TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                """)
                conn.commit()
    else:
        with _sqlite() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nexusai_sites (
                    site_id TEXT PRIMARY KEY, status TEXT, agent_version TEXT,
                    timestamp TEXT, received_at REAL, cameras_json TEXT
                );
                CREATE TABLE IF NOT EXISTS nexusai_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL,
                    camera_id TEXT, camera_name TEXT, location TEXT, event TEXT,
                    severity TEXT, timestamp TEXT, source TEXT, snapshot_available INTEGER
                );
                CREATE TABLE IF NOT EXISTS nexusai_push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE, subscription_json TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
            """)
            conn.commit()


@app.on_event("startup")
def startup():
    init_db()
    logging.info("NexusAI Cloud 3.0.0 started; database=%s", "postgres" if database_url() else "sqlite")


def push_configured() -> bool:
    return all(os.getenv(k, "").strip() for k in (
        "NEXUSAI_VAPID_PUBLIC_KEY",
        "NEXUSAI_VAPID_PRIVATE_KEY",
        "NEXUSAI_VAPID_SUBJECT",
        "NEXUSAI_APP_LINK_SECRET",
    ))


def app_link_secret() -> str:
    value = os.getenv("NEXUSAI_APP_LINK_SECRET", "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="NexusAI mobile installation is not configured.")
    return value


def create_install_token(site_id: str) -> str:
    now = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{site_id}.{now}.{nonce}"
    signature = hmac.new(app_link_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_install_token(site_id: str, token: str, max_age: int = 86400) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 4:
            return False
        token_site, issued, nonce, signature = parts
        if token_site != site_id:
            return False
        issued_int = int(issued)
        if abs(int(time.time()) - issued_int) > max_age:
            return False
        payload = f"{token_site}.{issued}.{nonce}"
        expected = hmac.new(app_link_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def save_push_subscription(site_id: str, subscription: dict):
    endpoint = str(subscription.get("endpoint", "")).strip()
    keys = subscription.get("keys") or {}
    if not endpoint or len(endpoint) > 4096 or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Invalid NexusAI push subscription.")

    now = time.time()
    payload = json.dumps(subscription, separators=(",", ":"))
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_push_subscriptions
                        (site_id, endpoint, subscription_json, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT(endpoint) DO UPDATE SET
                          site_id=EXCLUDED.site_id,
                          subscription_json=EXCLUDED.subscription_json,
                          updated_at=EXCLUDED.updated_at
                    """, (site_id, endpoint, payload, now, now))
                    conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("""
                    INSERT INTO nexusai_push_subscriptions
                    (site_id, endpoint, subscription_json, created_at, updated_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(endpoint) DO UPDATE SET
                      site_id=excluded.site_id,
                      subscription_json=excluded.subscription_json,
                      updated_at=excluded.updated_at
                """, (site_id, endpoint, payload, now, now))
                conn.commit()


def remove_push_subscription(endpoint: str):
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM nexusai_push_subscriptions WHERE endpoint=%s", (endpoint,))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("DELETE FROM nexusai_push_subscriptions WHERE endpoint=?", (endpoint,))
                conn.commit()


def load_push_subscriptions(site_id: str) -> list[tuple[str, dict]]:
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT endpoint, subscription_json FROM nexusai_push_subscriptions WHERE site_id=%s",
                        (site_id,),
                    )
                    rows = cur.fetchall()
        else:
            with _sqlite() as conn:
                rows = conn.execute(
                    "SELECT endpoint, subscription_json FROM nexusai_push_subscriptions WHERE site_id=?",
                    (site_id,),
                ).fetchall()
    result = []
    for row in rows:
        try:
            endpoint, raw = row[0], row[1]
            result.append((endpoint, json.loads(raw)))
        except Exception:
            continue
    return result


def persist_site(site: dict):
    payload = json.dumps(site.get("cameras", []), separators=(",", ":"))
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json)
                        VALUES(%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(site_id) DO UPDATE SET
                          status=EXCLUDED.status, agent_version=EXCLUDED.agent_version,
                          timestamp=EXCLUDED.timestamp, received_at=EXCLUDED.received_at,
                          cameras_json=EXCLUDED.cameras_json
                    """, (site["site_id"], site.get("status"), site.get("agent_version"),
                          site.get("timestamp"), site.get("received_at"), payload))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("""
                    INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(site_id) DO UPDATE SET
                      status=excluded.status, agent_version=excluded.agent_version,
                      timestamp=excluded.timestamp, received_at=excluded.received_at,
                      cameras_json=excluded.cameras_json
                """, (site["site_id"], site.get("status"), site.get("agent_version"),
                      site.get("timestamp"), site.get("received_at"), payload))
                conn.commit()


def persist_event(event: dict):
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_events
                        (site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (event["site_id"], event["camera_id"], event["camera_name"], event["location"],
                          event["event"], event["severity"], event["timestamp"], event["source"],
                          event.get("snapshot_available", False)))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("""
                    INSERT INTO nexusai_events
                    (site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (event["site_id"], event["camera_id"], event["camera_name"], event["location"],
                      event["event"], event["severity"], event["timestamp"], event["source"],
                      int(event.get("snapshot_available", False))))
                conn.commit()


def load_events(site_id: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 200))
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id,site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available
                        FROM nexusai_events WHERE site_id=%s ORDER BY id DESC LIMIT %s
                    """, (site_id, limit))
                    rows = cur.fetchall()
        else:
            with _sqlite() as conn:
                rows = conn.execute("""
                    SELECT id,site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available
                    FROM nexusai_events WHERE site_id=? ORDER BY id DESC LIMIT ?
                """, (site_id, limit)).fetchall()
    keys = ["id","site_id","camera_id","camera_name","location","event","severity","timestamp","source","snapshot_available"]
    return [dict(zip(keys, row)) for row in rows]


def push_once(subscription: dict, payload: str) -> tuple[bool, bool]:
    """Return (delivered, expired). Expired means the browser endpoint is no longer valid."""
    try:
        from pywebpush import webpush
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=os.getenv("NEXUSAI_VAPID_PRIVATE_KEY", "").strip(),
            vapid_claims={"sub": os.getenv("NEXUSAI_VAPID_SUBJECT", "").strip()},
            ttl=300,
            headers={"Urgency": "high"},
        )
        return True, False
    except Exception as exc:
        message = str(exc)
        expired = " 404 " in f" {message} " or " 410 " in f" {message} " or "410 Gone" in message or "404 Not Found" in message
        logging.warning("NexusAI push attempt failed: %s", message[:500])
        return False, expired


def deliver_push(endpoint: str, subscription: dict, payload: str) -> bool:
    # Bounded retries prevent a broken endpoint from consuming workers forever.
    # Each attempt uses a fresh Web Push request and increasing delay.
    for attempt in range(5):
        delivered, expired = push_once(subscription, payload)
        if delivered:
            return True
        if expired:
            remove_push_subscription(endpoint)
            logging.info("Removed expired NexusAI push subscription.")
            return False
        if attempt < 4:
            time.sleep(min(8, 2 ** attempt))
    return False


def dispatch_push_alert(event: dict):
    if not push_configured():
        logging.error("NexusAI push is not configured. Set VAPID keys and APP_LINK_SECRET on Render.")
        return

    subscriptions = load_push_subscriptions(event["site_id"])
    if not subscriptions:
        logging.info("No NexusAI mobile subscriptions for site %s", event["site_id"])
        return

    payload = json.dumps({
        "title": event.get("event", "NEXUSAI SECURITY ALERT"),
        "body": (
            f'Camera: {event.get("camera_name", "Protected camera")} • '
            f'Location: {event.get("location", "Client site")} • '
            f'Severity: {event.get("severity", "UNKNOWN")} • '
            f'Time: {event.get("timestamp", "")}'
        ),
        "event": event.get("event"),
        "camera": event.get("camera_name"),
        "location": event.get("location"),
        "severity": event.get("severity"),
        "timestamp": event.get("timestamp"),
        "tag": f'nexusai-{event.get("site_id")}-{event.get("camera_id")}-{time.time_ns()}',
        "url": f"/app/?site_id={quote(event.get('site_id', ''))}",
    }, separators=(",", ":"))

    futures = [
        PUSH_EXECUTOR.submit(deliver_push, endpoint, subscription, payload)
        for endpoint, subscription in subscriptions
    ]
    delivered = 0
    for future in as_completed(futures):
        try:
            if future.result():
                delivered += 1
        except Exception:
            logging.exception("NexusAI push worker failed.")
    logging.info("NexusAI push dispatch complete: %s/%s devices delivered.", delivered, len(futures))


def require_edge_token(token: str | None, authorization: str | None = None):
    expected = os.getenv("NEXUSAI_EDGE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="NexusAI Edge Agent authentication is not configured.")
    bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    supplied = token or bearer
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid NexusAI Edge Agent token.")


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


@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
async def nexusai_app():
    return FileResponse(APP_DIR / "index.html")


@app.get("/app/app.js", include_in_schema=False)
async def nexusai_app_js():
    return FileResponse(APP_DIR / "app.js", media_type="application/javascript")


@app.get("/app/service-worker.js", include_in_schema=False)
async def nexusai_app_sw():
    return FileResponse(
        APP_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/app/", "Cache-Control": "no-store"},
    )


@app.get("/app/manifest.json", include_in_schema=False)
async def nexusai_app_manifest():
    return FileResponse(APP_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/app/icon.svg", include_in_schema=False)
async def nexusai_app_icon():
    return FileResponse(APP_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/api/push/config")
async def push_config():
    return {
        "configured": push_configured(),
        "public_key": os.getenv("NEXUSAI_VAPID_PUBLIC_KEY", "").strip(),
    }


@app.get("/api/push/install-link")
async def push_install_link(site_id: str = "site-demo"):
    if not site_id or len(site_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid site ID.")
    token = create_install_token(site_id)
    return {
        "site_id": site_id,
        "url": f"/app/?site_id={quote(site_id)}&install_token={quote(token)}",
        "expires_in": 86400,
    }


@app.post("/api/push/subscribe")
async def push_subscribe(request: PushSubscriptionRequest):
    if not verify_install_token(request.site_id, request.install_token):
        raise HTTPException(status_code=403, detail="This NexusAI app installation link is invalid or expired.")
    if not push_configured():
        raise HTTPException(status_code=503, detail="NexusAI push service is not configured yet.")
    try:
        save_push_subscription(request.site_id, request.subscription)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"subscribed": True, "site_id": request.site_id}


@app.delete("/api/push/subscribe")
async def push_unsubscribe(request: PushSubscriptionRequest):
    endpoint = str(request.subscription.get("endpoint", "")).strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="Push endpoint required.")
    remove_push_subscription(endpoint)
    return {"unsubscribed": True}


@app.post("/api/push/test")
async def push_test(request: PushTestRequest):
    site_id = request.site_id
    install_token = request.install_token
    if not verify_install_token(site_id, install_token):
        raise HTTPException(status_code=403, detail="This NexusAI installation link is invalid or expired.")
    if not push_configured():
        raise HTTPException(status_code=503, detail="NexusAI push service is not configured yet.")
    test_event = {
        "site_id": site_id,
        "camera_id": "nexusai-test-camera",
        "camera_name": "NexusAI Test Camera",
        "location": "NexusAI Test Site",
        "event": "NEXUSAI TEST ALERT",
        "severity": "CRITICAL",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "nexusai-push-test",
        "snapshot_available": False,
    }
    PUSH_EXECUTOR.submit(dispatch_push_alert, test_event)
    return {"accepted": True, "notification": "NEXUSAI_TEST_PUSH_QUEUED"}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "NexusAI Cloud",
        "version": "3.0.0",
        "push": "configured" if push_configured() else "awaiting-vapid-keys",
    }


@app.get("/api/status")
async def api_status():
    return {
        "nexusai": "ONLINE",
        "security_engine": "READY",
        "camera_verification": "EDGE_AGENT",
        "notification_engine": "NEXUSAI_PUSH",
        "whatsapp": "REMOVED",
        "api_version": "3.0.0",
    }


@app.post("/api/edge/heartbeat")
async def edge_heartbeat(
    heartbeat: EdgeHeartbeat,
    x_nexusai_edge_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token, authorization)
    site = {
        "site_id": heartbeat.site_id,
        "status": heartbeat.status,
        "agent_version": heartbeat.agent_version,
        "timestamp": heartbeat.timestamp,
        "received_at": time.time(),
        "cameras": [camera.model_dump() for camera in heartbeat.cameras],
    }
    EDGE_SITES[heartbeat.site_id] = site
    persist_site(site)
    return {"accepted": True, "service": "NexusAI Edge Agent", "site_id": heartbeat.site_id, "status": "ONLINE"}


@app.post("/api/edge/verify")
async def edge_verify(
    verification: EdgeVerification,
    x_nexusai_edge_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token, authorization)
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
    authorization: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token, authorization)
    event_data = event.model_dump()
    EDGE_EVENTS.insert(0, event_data)
    del EDGE_EVENTS[200:]
    persist_event(event_data)

    # The Edge Agent gets an immediate acknowledgement. Push delivery runs
    # in the server-side dispatcher so the camera path is never blocked by a phone.
    PUSH_EXECUTOR.submit(dispatch_push_alert, event_data)

    return {
        "accepted": True,
        "site_id": event.site_id,
        "camera_id": event.camera_id,
        "event": event.event,
        "severity": event.severity,
        "timestamp": event.timestamp,
        "notification": "NEXUSAI_PUSH_QUEUED",
    }


@app.get("/api/portal/status")
async def portal_status(site_id: str = "site-demo"):
    site = EDGE_SITES.get(site_id)
    if not site:
        site = load_site(site_id)
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
    return {"site_id": site_id, "events": load_events(site_id, limit)}


def load_site(site_id: str):
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT site_id,status,agent_version,timestamp,received_at,cameras_json FROM nexusai_sites WHERE site_id=%s",
                        (site_id,),
                    )
                    row = cur.fetchone()
        else:
            with _sqlite() as conn:
                row = conn.execute(
                    "SELECT site_id,status,agent_version,timestamp,received_at,cameras_json FROM nexusai_sites WHERE site_id=?",
                    (site_id,),
                ).fetchone()
    if not row:
        return None
    return {
        "site_id": row[0],
        "status": row[1],
        "agent_version": row[2],
        "timestamp": row[3],
        "received_at": row[4],
        "cameras": json.loads(row[5] or "[]"),
    }


# Keep the portal mounted last so API routes stay reachable.
app.mount("/portal", StaticFiles(directory=PORTAL_DIR, html=True), name="portal")
