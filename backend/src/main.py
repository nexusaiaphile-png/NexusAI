import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, PlainTextResponse, Response
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
ACTIVATION_RATE: dict[str, list[float]] = {}
ACTIVATION_RATE_LOCK = threading.Lock()
SITE_ID_RE = re.compile(r"^site-[A-Za-z0-9._-]{6,100}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(
    title="NexusAI Security Cloud",
    description="NexusAI Hikvision security cloud and native mobile push notification service.",
    version="3.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://getnexusai.co.za", "https://www.getnexusai.co.za"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def nexusai_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class EdgeCamera(BaseModel):
    camera_id: str = Field(..., min_length=1, max_length=128)
    camera_name: str = Field(..., min_length=1, max_length=200)
    location: str = Field(..., min_length=1, max_length=200)
    verified: bool = False
    device_id: str | None = None
    device_ip: str | None = None
    channel_id: str | None = None
    device_type: str | None = None
    status: str = Field(default="ONLINE", max_length=32)
    capabilities: dict = Field(default_factory=dict)


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
    capabilities: dict = Field(default_factory=dict)


class EdgeEvent(BaseModel):
    site_id: str = Field(..., min_length=8, max_length=100)
    camera_id: str = Field(..., min_length=1, max_length=128)
    camera_name: str = Field(..., min_length=1, max_length=200)
    location: str = Field(..., min_length=1, max_length=200)
    event: str = Field(..., min_length=1, max_length=120)
    severity: str = Field(..., min_length=1, max_length=32)
    timestamp: str = Field(..., min_length=1, max_length=80)
    source: str = Field(..., min_length=1, max_length=80)
    snapshot_available: bool = False
    snapshot_base64: str | None = Field(default=None, max_length=3_000_000)
    snapshot_mime: str | None = None
    snapshot_filename: str | None = None


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
                        snapshot_available BOOLEAN,
                        snapshot_mime TEXT,
                        snapshot_filename TEXT,
                        snapshot_data BYTEA
                    )
                """)
                cur.execute("ALTER TABLE nexusai_qr_tokens ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVATED'")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_mime TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_filename TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_data BYTEA")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_cameras (
                        site_id TEXT NOT NULL,
                        camera_id TEXT NOT NULL,
                        device_id TEXT,
                        device_ip TEXT,
                        channel_id TEXT,
                        camera_name TEXT,
                        location TEXT,
                        device_type TEXT,
                        verified BOOLEAN NOT NULL DEFAULT FALSE,
                        status TEXT NOT NULL DEFAULT 'ONLINE',
                        last_seen DOUBLE PRECISION,
                        metadata_json TEXT,
                        PRIMARY KEY(site_id, camera_id)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_cameras_site ON nexusai_cameras(site_id)")
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
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_site_codes (
                        activation_code TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL UNIQUE,
                        created_at DOUBLE PRECISION NOT NULL,
                        last_used_at DOUBLE PRECISION
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_qr_tokens (
                        qr_token TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL UNIQUE,
                        created_at DOUBLE PRECISION NOT NULL,
                        last_used_at DOUBLE PRECISION,
                        status TEXT NOT NULL DEFAULT 'ACTIVATED'
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_qr_site ON nexusai_qr_tokens(site_id)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_installer_tokens (
                        token_hash TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL,
                        used_at DOUBLE PRECISION
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_installer_site ON nexusai_installer_tokens(site_id)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_customers (
                        site_id TEXT PRIMARY KEY,
                        business_name TEXT NOT NULL,
                        store_address TEXT,
                        contact_name TEXT,
                        contact_phone TEXT,
                        contact_email TEXT,
                        camera_count INTEGER NOT NULL DEFAULT 0,
                        created_at DOUBLE PRECISION NOT NULL,
                        status TEXT NOT NULL DEFAULT 'ACTIVE'
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS nexusai_customer_tokens (
                        token_hash TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_customer_tokens_site ON nexusai_customer_tokens(site_id)")

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
                    severity TEXT, timestamp TEXT, source TEXT, snapshot_available INTEGER,
                    snapshot_mime TEXT, snapshot_filename TEXT, snapshot_data BLOB
                );
                CREATE TABLE IF NOT EXISTS nexusai_cameras (
                    site_id TEXT NOT NULL, camera_id TEXT NOT NULL,
                    device_id TEXT, device_ip TEXT, channel_id TEXT,
                    camera_name TEXT, location TEXT, device_type TEXT,
                    verified INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ONLINE',
                    last_seen REAL, metadata_json TEXT,
                    PRIMARY KEY(site_id, camera_id)
                );
                CREATE INDEX IF NOT EXISTS idx_nexusai_cameras_site ON nexusai_cameras(site_id);
                CREATE TABLE IF NOT EXISTS nexusai_push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE, subscription_json TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nexusai_site_codes (
                    activation_code TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    last_used_at REAL
                );
                CREATE TABLE IF NOT EXISTS nexusai_qr_tokens (
                    qr_token TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    status TEXT NOT NULL DEFAULT 'ACTIVATED'
                );
                CREATE INDEX IF NOT EXISTS idx_nexusai_qr_site ON nexusai_qr_tokens(site_id);
                CREATE TABLE IF NOT EXISTS nexusai_installer_tokens (
                    token_hash TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_nexusai_installer_site ON nexusai_installer_tokens(site_id);
                CREATE TABLE IF NOT EXISTS nexusai_customers (
                    site_id TEXT PRIMARY KEY,
                    business_name TEXT NOT NULL,
                    store_address TEXT,
                    contact_name TEXT,
                    contact_phone TEXT,
                    contact_email TEXT,
                    camera_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE'
                );
                CREATE TABLE IF NOT EXISTS nexusai_customer_tokens (
                    token_hash TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_nexusai_customer_tokens_site ON nexusai_customer_tokens(site_id);

            """)
            conn.commit()


@app.on_event("startup")
def startup():
    init_db()
    if database_url():
        import psycopg
        with psycopg.connect(database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE nexusai_qr_tokens ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVATED'")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_mime TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_filename TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_data BYTEA")
            conn.commit()
    else:
        with _sqlite() as conn:
            qr_cols = {row[1] for row in conn.execute("PRAGMA table_info(nexusai_qr_tokens)").fetchall()}
            if "status" not in qr_cols:
                conn.execute("ALTER TABLE nexusai_qr_tokens ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVATED'")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(nexusai_events)").fetchall()}
            if "snapshot_mime" not in cols: conn.execute("ALTER TABLE nexusai_events ADD COLUMN snapshot_mime TEXT")
            if "snapshot_filename" not in cols: conn.execute("ALTER TABLE nexusai_events ADD COLUMN snapshot_filename TEXT")
            if "snapshot_data" not in cols: conn.execute("ALTER TABLE nexusai_events ADD COLUMN snapshot_data BLOB")
            conn.commit()
    logging.info("NexusAI Cloud 3.1.0 started; database=%s", "postgres" if database_url() else "sqlite")


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
    cameras=site.get("cameras",[])
    payload=json.dumps(cameras,separators=(",",":"))
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json)
                        VALUES(%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(site_id) DO UPDATE SET
                          status=EXCLUDED.status,agent_version=EXCLUDED.agent_version,
                          timestamp=EXCLUDED.timestamp,received_at=EXCLUDED.received_at
                    """,(site["site_id"],site.get("status"),site.get("agent_version"),site.get("timestamp"),site.get("received_at"),payload))
                    device_ids={str(c.get("device_id")) for c in cameras if c.get("device_id")}
                    for camera in cameras:
                        cur.execute("""
                            INSERT INTO nexusai_cameras
                            (site_id,camera_id,device_id,device_ip,channel_id,camera_name,location,device_type,verified,status,last_seen,metadata_json)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(site_id,camera_id) DO UPDATE SET
                              device_id=EXCLUDED.device_id,device_ip=EXCLUDED.device_ip,channel_id=EXCLUDED.channel_id,
                              camera_name=EXCLUDED.camera_name,location=EXCLUDED.location,device_type=EXCLUDED.device_type,
                              verified=EXCLUDED.verified,status=EXCLUDED.status,last_seen=EXCLUDED.last_seen,metadata_json=EXCLUDED.metadata_json
                        """,(site["site_id"],str(camera.get("camera_id","")),camera.get("device_id"),camera.get("device_ip"),
                             camera.get("channel_id"),camera.get("camera_name"),camera.get("location"),camera.get("device_type"),
                             bool(camera.get("verified")),camera.get("status","ONLINE"),site.get("received_at"),json.dumps(camera,separators=(",",":"))))
                    for device_id in device_ids:
                        incoming_ids={str(c.get("camera_id")) for c in cameras if str(c.get("device_id"))==device_id}
                        if incoming_ids:
                            cur.execute(
                                "DELETE FROM nexusai_cameras WHERE site_id=%s AND device_id=%s AND camera_id <> ALL(%s)",
                                (site["site_id"],device_id,list(incoming_ids))
                            )
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("""
                    INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(site_id) DO UPDATE SET
                      status=excluded.status,agent_version=excluded.agent_version,
                      timestamp=excluded.timestamp,received_at=excluded.received_at
                """,(site["site_id"],site.get("status"),site.get("agent_version"),site.get("timestamp"),site.get("received_at"),payload))
                device_ids={str(c.get("device_id")) for c in cameras if c.get("device_id")}
                for camera in cameras:
                    conn.execute("""
                        INSERT INTO nexusai_cameras
                        (site_id,camera_id,device_id,device_ip,channel_id,camera_name,location,device_type,verified,status,last_seen,metadata_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(site_id,camera_id) DO UPDATE SET
                          device_id=excluded.device_id,device_ip=excluded.device_ip,channel_id=excluded.channel_id,
                          camera_name=excluded.camera_name,location=excluded.location,device_type=excluded.device_type,
                          verified=excluded.verified,status=excluded.status,last_seen=excluded.last_seen,metadata_json=excluded.metadata_json
                    """,(site["site_id"],str(camera.get("camera_id","")),camera.get("device_id"),camera.get("device_ip"),
                         camera.get("channel_id"),camera.get("camera_name"),camera.get("location"),camera.get("device_type"),
                         int(bool(camera.get("verified"))),camera.get("status","ONLINE"),site.get("received_at"),json.dumps(camera,separators=(",",":"))))
                for device_id in device_ids:
                    incoming_ids={str(c.get("camera_id")) for c in cameras if str(c.get("device_id"))==device_id}
                    if incoming_ids:
                        placeholders=",".join("?" for _ in incoming_ids)
                        conn.execute(
                            f"DELETE FROM nexusai_cameras WHERE site_id=? AND device_id=? AND camera_id NOT IN ({placeholders})",
                            (site["site_id"],device_id,*incoming_ids)
                        )
                conn.commit()

def load_cameras(site_id: str) -> list[dict]:
    cutoff=time.time()-120
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT camera_id,device_id,device_ip,channel_id,camera_name,location,device_type,verified,status,last_seen FROM nexusai_cameras WHERE site_id=%s ORDER BY camera_name",(site_id,))
                    rows=cur.fetchall()
        else:
            with _sqlite() as conn:
                rows=conn.execute("SELECT camera_id,device_id,device_ip,channel_id,camera_name,location,device_type,verified,status,last_seen FROM nexusai_cameras WHERE site_id=? ORDER BY camera_name",(site_id,)).fetchall()
    return [{
        "camera_id":r[0],"device_id":r[1],"device_ip":r[2],"channel_id":r[3],"camera_name":r[4],
        "location":r[5],"device_type":r[6],"verified":bool(r[7]),
        "status":r[8] if r[9] and float(r[9])>=cutoff else "OFFLINE","last_seen":r[9]
    } for r in rows]

def _decode_snapshot(event: dict):
    raw=event.get("snapshot_base64")
    if not raw: return None,None,None
    try: data=base64.b64decode(raw,validate=True)
    except (binascii.Error,ValueError): return None,None,None
    if len(data)>2*1024*1024: return None,None,None
    mime=str(event.get("snapshot_mime") or "image/jpeg").split(";")[0].strip().lower()
    if mime not in {"image/jpeg","image/png","image/webp"}: mime="image/jpeg"
    filename=str(event.get("snapshot_filename") or "snapshot.jpg")[:180]
    return data,mime,filename

def persist_event(event: dict):
    snapshot_data,snapshot_mime,snapshot_filename=_decode_snapshot(event)
    snapshot_available=bool(snapshot_data)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_events
                        (site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available,snapshot_mime,snapshot_filename,snapshot_data)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,(event["site_id"],event["camera_id"],event["camera_name"],event["location"],event["event"],event["severity"],
                         event["timestamp"],event["source"],snapshot_available,snapshot_mime,snapshot_filename,snapshot_data))
                    event_id=cur.fetchone()[0]
                conn.commit()
        else:
            with _sqlite() as conn:
                cur=conn.execute("""
                    INSERT INTO nexusai_events
                    (site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available,snapshot_mime,snapshot_filename,snapshot_data)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,(event["site_id"],event["camera_id"],event["camera_name"],event["location"],event["event"],event["severity"],
                     event["timestamp"],event["source"],int(snapshot_available),snapshot_mime,snapshot_filename,snapshot_data))
                event_id=cur.lastrowid
                conn.commit()
    event["id"]=event_id
    event["snapshot_available"]=snapshot_available
    event.pop("snapshot_base64",None)
    return event_id

def load_events(site_id: str, limit: int = 50, access_code: str | None = None) -> list[dict]:
    limit=max(1,min(limit,200))
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available FROM nexusai_events WHERE site_id=%s ORDER BY id DESC LIMIT %s",(site_id,limit))
                    rows=cur.fetchall()
        else:
            with _sqlite() as conn:
                rows=conn.execute("SELECT id,site_id,camera_id,camera_name,location,event,severity,timestamp,source,snapshot_available FROM nexusai_events WHERE site_id=? ORDER BY id DESC LIMIT ?",(site_id,limit)).fetchall()
    keys=["id","site_id","camera_id","camera_name","location","event","severity","timestamp","source","snapshot_available"]
    result=[]
    for row in rows:
        item=dict(zip(keys,row))
        if item.get("snapshot_available"): item["snapshot_url"]=f"/api/portal/snapshots/{item['id']}?site_id={quote(site_id)}&access_code={quote(access_code or '')}"
        result.append(item)
    return result

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
        "snapshot_available": bool(event.get("snapshot_available")),
        "snapshot_url": (f"/api/portal/snapshots/{event.get('id')}?site_id={quote(event.get('site_id',''))}" if event.get("id") and event.get("snapshot_available") else None),
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



def _customer_token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

def create_customer_token(site_id: str) -> str:
    token = "nxc_" + secrets.token_urlsafe(36)
    now = time.time()
    token_hash = _customer_token_hash(token)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO nexusai_customer_tokens(token_hash,site_id,created_at,active) VALUES(%s,%s,%s,TRUE)", (token_hash, site_id, now))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("INSERT INTO nexusai_customer_tokens(token_hash,site_id,created_at,active) VALUES(?,?,?,1)", (token_hash, site_id, now))
                conn.commit()
    return token

def require_customer_token(token: str) -> str:
    raw = str(token or "").strip()
    if len(raw) < 30 or len(raw) > 200:
        raise HTTPException(status_code=401, detail="NexusAI customer access is invalid.")
    token_hash = _customer_token_hash(raw)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT site_id FROM nexusai_customer_tokens WHERE token_hash=%s AND active=TRUE", (token_hash,))
                    row = cur.fetchone()
        else:
            with _sqlite() as conn:
                row = conn.execute("SELECT site_id FROM nexusai_customer_tokens WHERE token_hash=? AND active=1", (token_hash,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="NexusAI customer access is invalid or has been disabled.")
    return validate_site_id(row[0])

def load_customer(site_id: str):
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT site_id,business_name,store_address,contact_name,contact_phone,contact_email,camera_count,created_at,status FROM nexusai_customers WHERE site_id=%s", (site_id,))
                    row=cur.fetchone()
        else:
            with _sqlite() as conn:
                row=conn.execute("SELECT site_id,business_name,store_address,contact_name,contact_phone,contact_email,camera_count,created_at,status FROM nexusai_customers WHERE site_id=?", (site_id,)).fetchone()
    if not row:
        return None
    keys=["site_id","business_name","store_address","contact_name","contact_phone","contact_email","camera_count","created_at","status"]
    return dict(zip(keys,row))

def save_customer(data: dict):
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO nexusai_customers(site_id,business_name,store_address,contact_name,contact_phone,contact_email,camera_count,created_at,status)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE')
                        ON CONFLICT(site_id) DO UPDATE SET business_name=EXCLUDED.business_name,store_address=EXCLUDED.store_address,
                        contact_name=EXCLUDED.contact_name,contact_phone=EXCLUDED.contact_phone,contact_email=EXCLUDED.contact_email,camera_count=EXCLUDED.camera_count,status='ACTIVE'
                    """,(data["site_id"],data["business_name"],data.get("store_address",""),data.get("contact_name",""),data.get("contact_phone",""),data.get("contact_email",""),int(data.get("camera_count") or 0),time.time()))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("""
                    INSERT INTO nexusai_customers(site_id,business_name,store_address,contact_name,contact_phone,contact_email,camera_count,created_at,status)
                    VALUES(?,?,?,?,?,?,?,?,'ACTIVE')
                    ON CONFLICT(site_id) DO UPDATE SET business_name=excluded.business_name,store_address=excluded.store_address,
                    contact_name=excluded.contact_name,contact_phone=excluded.contact_phone,contact_email=excluded.contact_email,camera_count=excluded.camera_count,status='ACTIVE'
                """,(data["site_id"],data["business_name"],data.get("store_address",""),data.get("contact_name",""),data.get("contact_phone",""),data.get("contact_email",""),int(data.get("camera_count") or 0),time.time()))
                conn.commit()

def admin_site_summary(site_id: str):
    customer=load_customer(site_id) or {"site_id":site_id,"business_name":"NexusAI Site","store_address":"","contact_name":"","contact_phone":"","contact_email":"","camera_count":0,"status":"ACTIVE"}
    site=EDGE_SITES.get(site_id) or load_site(site_id) or {"site_id":site_id,"status":"WAITING","agent_version":"","received_at":0,"cameras":[]}
    cameras=load_cameras(site_id)
    return {**customer,"edge_agent":"ONLINE" if site.get("received_at") and time.time()-float(site.get("received_at",0))<=90 else "OFFLINE",
            "site_status":site.get("status","WAITING"),"agent_version":site.get("agent_version",""),"cameras":cameras,
            "camera_count_live":len(cameras)}

def validate_site_id(site_id: str) -> str:
    value = str(site_id or "").strip()
    if not SITE_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid NexusAI site ID.")
    return value


def require_site_access(site_id: str, activation_code: str | None = None):
    site_id = validate_site_id(site_id)
    code = (activation_code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=401, detail="NexusAI site activation code required.")
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM nexusai_site_codes WHERE activation_code=%s AND site_id=%s", (code, site_id))
                    ok = cur.fetchone() is not None
        else:
            with _sqlite() as conn:
                ok = conn.execute("SELECT 1 FROM nexusai_site_codes WHERE activation_code=? AND site_id=?", (code, site_id)).fetchone() is not None
    if not ok:
        raise HTTPException(status_code=403, detail="Invalid NexusAI site activation code.")
    return site_id


def allow_activation_attempt(request: Request):
    host = request.client.host if request.client else "unknown"
    now = time.time()
    with ACTIVATION_RATE_LOCK:
        attempts = [t for t in ACTIVATION_RATE.get(host, []) if now - t < 600]
        if len(attempts) >= 30:
            raise HTTPException(status_code=429, detail="Too many activation attempts. Please wait and try again.")
        attempts.append(now)
        ACTIVATION_RATE[host] = attempts


def site_edge_token(site_id: str) -> str:
    secret = os.getenv("NEXUSAI_APP_LINK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="NexusAI site security is not configured.")
    site_id = validate_site_id(site_id)
    payload = "nexusai-edge:" + site_id
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def require_edge_token(token: str | None, authorization: str | None = None, site_id: str | None = None):
    bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    supplied = token or bearer
    secret = os.getenv("NEXUSAI_APP_LINK_SECRET", "").strip()
    if site_id and secret and supplied:
        expected_site = hmac.new(secret.encode(), ("nexusai-edge:" + validate_site_id(site_id)).encode(), hashlib.sha256).hexdigest()
        if secrets.compare_digest(supplied, expected_site):
            return
    expected = os.getenv("NEXUSAI_EDGE_TOKEN", "").strip()
    if expected and supplied and secrets.compare_digest(supplied, expected):
        return
    if not expected and not secret:
        raise HTTPException(status_code=503, detail="NexusAI Edge Agent authentication is not configured.")
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


@app.get("/protect", include_in_schema=False)
@app.get("/protect/", include_in_schema=False)
async def simple_protect():
    return FileResponse(PORTAL_DIR / "protect.html")

@app.get("/protect/style.css", include_in_schema=False)
async def protect_style():
    return FileResponse(PORTAL_DIR / "protect.css", media_type="text/css")

@app.get("/protect/print", include_in_schema=False)
async def protect_print():
    return FileResponse(PORTAL_DIR / "qr.html")

@app.get("/qr-pool", include_in_schema=False)
async def qr_pool_page():
    return FileResponse(PORTAL_DIR / "qr-pool.html")

@app.get("/protect/app.js", include_in_schema=False)
async def protect_app_js():
    return FileResponse(PORTAL_DIR / "protect/app.js", media_type="application/javascript")



@app.get("/admin", include_in_schema=False)
async def nexusai_admin_portal():
    return FileResponse(PORTAL_DIR / "admin" / "index.html", media_type="text/html")

@app.get("/client", include_in_schema=False)
@app.get("/client/", include_in_schema=False)
async def nexusai_client_portal():
    return FileResponse(PORTAL_DIR / "client" / "index.html", media_type="text/html")

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


EDGE_AGENT_DIR = BASE_DIR / "edge_agent"

SECURITY_BOX_RELEASE_TAG = os.getenv("NEXUSAI_SECURITY_BOX_RELEASE_TAG", "security-box-latest")
SECURITY_BOX_RELEASE_BASE = "https://github.com/nexusaiaphile-png/NexusAI/releases/download/" + SECURITY_BOX_RELEASE_TAG

def _download_security_box_asset(filename: str) -> bytes:
    url = SECURITY_BOX_RELEASE_BASE + "/" + filename
    try:
        request = UrlRequest(url, headers={"User-Agent": "NexusAI-Security-Cloud"})
        with urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as exc:
        logging.exception("Could not download Security Box release asset: %s", filename)
        raise HTTPException(status_code=503, detail="NexusAI Security Box installer is not available yet.") from exc
    if not data:
        raise HTTPException(status_code=503, detail="NexusAI Security Box installer is empty.")
    return data

def _validate_installer_download_token(token: str) -> str:
    # Validate the signed token without consuming it; the downloaded installer
    # still needs the same token to bootstrap the Security Box.
    verify_installer_token(token, max_age=900)
    return str(token).strip()

@app.get("/protect/install", include_in_schema=False)
async def protect_install_page():
    return FileResponse(PORTAL_DIR / "protect" / "install.html", media_type="text/html")

@app.get("/downloads/security-box/source/{filename}", include_in_schema=False)
async def security_box_source(filename: str):
    allowed = {
        "agent.py": ("agent.py", "text/x-python"),
        "security_box.py": ("security_box.py", "text/x-python"),
        "secure_store.py": ("secure_store.py", "text/x-python"),
        "setup_security_box.py": ("setup_security_box.py", "text/x-python"),
        "windows_service.py": ("windows_service.py", "text/x-python"),
    }
    item = allowed.get(filename)
    if not item:
        raise HTTPException(status_code=404, detail="Security Box file not found.")
    return FileResponse(EDGE_AGENT_DIR / item[0], media_type=item[1], headers={"Cache-Control":"no-store"})

@app.get("/downloads/security-box/windows.exe", include_in_schema=False)
async def security_box_windows_exe(installer_token: str):
    token = _validate_installer_download_token(installer_token)
    payload = _download_security_box_asset("NexusAI-SecurityBox-Windows.exe")
    return Response(
        content=payload,
        media_type="application/vnd.microsoft.portable-executable",
        headers={
            "Content-Disposition": f'attachment; filename="NexusAI-SecurityBox-{token}.exe"',
            "Cache-Control": "no-store",
        },
    )

@app.get("/downloads/security-box/macos.pkg", include_in_schema=False)
async def security_box_macos_pkg(installer_token: str, arch: str = "arm64"):
    token = _validate_installer_download_token(installer_token)
    normalized = str(arch or "").lower()
    if normalized in {"arm64", "aarch64"}:
        asset = "NexusAI-SecurityBox-Mac-Arm64.pkg"
        label = "Arm64"
    elif normalized in {"x86_64", "intel", "x64"}:
        asset = "NexusAI-SecurityBox-Mac-Intel.pkg"
        label = "Intel"
    else:
        raise HTTPException(status_code=400, detail="Unsupported Mac architecture.")
    payload = _download_security_box_asset(asset)
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="NexusAI-SecurityBox-Mac-{label}-{token}.pkg"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/downloads/security-box/windows.ps1", include_in_schema=False)
async def security_box_windows_installer(installer_token: str):
    token = str(installer_token or "").strip()
    if len(token) < 40 or len(token) > 220:
        raise HTTPException(status_code=400, detail="Invalid installer token.")
    script = r'''$ErrorActionPreference = "Stop"
if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
  $PythonInstaller = "$env:TEMP\NexusAI-Python-3.13.16.exe"
  Invoke-WebRequest "https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe" -OutFile $PythonInstaller
  Start-Process $PythonInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  Remove-Item $PythonInstaller -Force
}
$Py = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $Py) { throw "Python could not be installed." }
$Root = "$env:ProgramFiles\NexusAI\SecurityBox"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "edge_agent") | Out-Null
foreach ($Name in @("agent.py","security_box.py","secure_store.py","setup_security_box.py")) {
  Invoke-WebRequest ("https://getnexusai.co.za/downloads/security-box/source/" + $Name) -OutFile (Join-Path $Root ("edge_agent\" + $Name))
}
Invoke-WebRequest "https://getnexusai.co.za/downloads/security-box/source/windows_service.py" -OutFile (Join-Path $Root "windows_service.py")
& $Py.Source -m pip install --disable-pip-version-check --quiet requests python-dotenv pywin32
& $Py.Source (Join-Path $Root "edge_agent\setup_security_box.py") --installer-token "__TOKEN__"
& $Py.Source (Join-Path $Root "windows_service.py") install
sc.exe config NexusAISecurityBox start= delayed-auto | Out-Null
sc.exe failure NexusAISecurityBox reset= 900 actions= restart/60000/restart/120000/restart/300000 | Out-Null
& $Py.Source (Join-Path $Root "windows_service.py") start
Write-Host "NexusAI Security Box is installed and running."
'''
    return PlainTextResponse(script.replace("__TOKEN__", token), media_type="text/plain", headers={"Content-Disposition":'attachment; filename="NexusAI-SecurityBox-Installer.ps1"',"Cache-Control":"no-store"})

@app.get("/downloads/security-box/macos.sh", include_in_schema=False)
async def security_box_macos_installer(installer_token: str):
    token = str(installer_token or "").strip()
    if len(token) < 40 or len(token) > 120:
        raise HTTPException(status_code=400, detail="Invalid installer token.")
    script = r'''#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0"
fi
ROOT="/Library/Application Support/NexusAI/SecurityBox"
mkdir -p "$ROOT"
PY="/usr/local/bin/python3"
if [ ! -x "$PY" ]; then
  PKG="/tmp/NexusAI-Python-3.13.16.pkg"
  curl -fL "https://www.python.org/ftp/python/3.13.16/python-3.13.16-macos11.pkg" -o "$PKG"
  installer -pkg "$PKG" -target /
  rm -f "$PKG"
fi
mkdir -p "$ROOT/edge_agent"
for NAME in agent.py security_box.py secure_store.py setup_security_box.py; do
  curl -fL "https://getnexusai.co.za/downloads/security-box/source/$NAME" -o "$ROOT/edge_agent/$NAME"
done
"$PY" -m pip install --disable-pip-version-check --quiet requests python-dotenv
"$PY" "$ROOT/edge_agent/setup_security_box.py" --installer-token "__TOKEN__"
EXEC="$ROOT/NexusAI-SecurityBox"
cat > "$EXEC" <<'PYEOF'
#!/usr/bin/env python3
from edge_agent.security_box import run
raise SystemExit(run())
PYEOF
chmod 755 "$EXEC"
PLIST="/Library/LaunchDaemons/za.co.getnexusai.securitybox.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>za.co.getnexusai.securitybox</string>
<key>ProgramArguments</key><array><string>$EXEC</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ProcessType</key><string>Background</string>
<key>StandardOutPath</key><string>/Library/Application Support/NexusAI/SecurityBox/security-box.stdout.log</string>
<key>StandardErrorPath</key><string>/Library/Application Support/NexusAI/SecurityBox/security-box.stderr.log</string>
</dict></plist>
EOF
chmod 644 "$PLIST"
chown root:wheel "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
launchctl enable system/za.co.getnexusai.securitybox
launchctl kickstart -k system/za.co.getnexusai.securitybox
echo "NexusAI Security Box is installed and running."
'''
    return PlainTextResponse(script.replace("__TOKEN__", token), media_type="text/plain", headers={"Content-Disposition":'attachment; filename="NexusAI-SecurityBox-Installer.sh"',"Cache-Control":"no-store"})



@app.get("/downloads/edge-agent/agent.py", include_in_schema=False)
async def download_edge_agent_source():
    return FileResponse(EDGE_AGENT_DIR / "agent.py", media_type="text/x-python", headers={"Cache-Control":"no-store"})


@app.get("/downloads/edge-agent/requirements.txt", include_in_schema=False)
async def download_edge_agent_requirements():
    return FileResponse(EDGE_AGENT_DIR / "requirements.txt", media_type="text/plain", headers={"Cache-Control":"no-store"})


class SiteActivationRequest(BaseModel):
    activation_code: str | None = Field(default=None, min_length=6, max_length=32)


def _new_site_id() -> str:
    return "site-" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")


def _new_activation_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "NEX-" + "".join(secrets.choice(alphabet) for _ in range(8))


def _new_qr_token() -> str:
    return secrets.token_urlsafe(32).replace("-", "").replace("_", "")


def _pool_placeholder(token: str) -> str:
    return "__POOL__:" + token


def ensure_qr_token(site_id: str) -> str:
    site_id = validate_site_id(site_id)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT qr_token FROM nexusai_qr_tokens WHERE site_id=%s AND status='ACTIVATED'", (site_id,))
                    row = cur.fetchone()
                    if row:
                        return row[0]
                    token = _new_qr_token()
                    cur.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at,last_used_at,status) VALUES(%s,%s,%s,%s,'ACTIVATED')", (token,site_id,time.time(),None))
                conn.commit()
                return token
        with _sqlite() as conn:
            row = conn.execute("SELECT qr_token FROM nexusai_qr_tokens WHERE site_id=? AND status='ACTIVATED'", (site_id,)).fetchone()
            if row:
                return row[0]
            for _ in range(10):
                token = _new_qr_token()
                try:
                    conn.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at,last_used_at,status) VALUES(?,?,?,?,?)", (token,site_id,time.time(),None,"ACTIVATED"))
                    conn.commit()
                    return token
                except sqlite3.IntegrityError:
                    conn.rollback()
            raise HTTPException(status_code=500, detail="Could not create NexusAI QR token.")


def normalize_qr_token(qr_token: str) -> str:
    value = unquote(str(qr_token or "").strip())
    if value.startswith(("https://", "http://")):
        try:
            parsed = urlsplit(value)
            value = parse_qs(parsed.query).get("qr", [""])[0]
        except Exception:
            value = ""
    elif value.startswith("qr="):
        value = value[3:]
    return unquote(str(value).strip()).strip().rstrip(".,;)")


def validate_qr_token(qr_token: str) -> str:
    token = normalize_qr_token(qr_token)
    if len(token) < 20 or len(token) > 120 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise HTTPException(status_code=400, detail="Invalid NexusAI QR code.")
    return token


def qr_token_info(qr_token: str) -> dict:
    token = validate_qr_token(qr_token)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT qr_token,site_id,status,created_at,last_used_at FROM nexusai_qr_tokens WHERE qr_token=%s", (token,))
                    row = cur.fetchone()
        else:
            with _sqlite() as conn:
                row = conn.execute("SELECT qr_token,site_id,status,created_at,last_used_at FROM nexusai_qr_tokens WHERE qr_token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="NexusAI QR code not found.")
    return {"qr_token":row[0],"site_id":row[1],"status":row[2] or "ACTIVATED","created_at":row[3],"last_used_at":row[4]}


def resolve_qr_token(qr_token: str) -> str:
    info = qr_token_info(qr_token)
    if info["status"] != "ACTIVATED" or not info["site_id"] or str(info["site_id"]).startswith("__POOL__:"):
        raise HTTPException(status_code=409, detail="This NexusAI QR sticker has not been activated yet.")
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE nexusai_qr_tokens SET last_used_at=%s WHERE qr_token=%s AND status='ACTIVATED'", (time.time(),info["qr_token"]))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("UPDATE nexusai_qr_tokens SET last_used_at=? WHERE qr_token=? AND status='ACTIVATED'", (time.time(),info["qr_token"]))
                conn.commit()
    return str(info["site_id"])


def claim_qr_for_new_site(qr_token: str) -> tuple[str, str]:
    token = validate_qr_token(qr_token)
    now = time.time()
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT site_id,status FROM nexusai_qr_tokens WHERE qr_token=%s FOR UPDATE", (token,))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="NexusAI QR code not found.")
                    if row[1] == "ACTIVATED" and row[0] and not str(row[0]).startswith("__POOL__:"):
                        cur.execute("UPDATE nexusai_qr_tokens SET last_used_at=%s WHERE qr_token=%s", (now,token))
                        cur.execute("SELECT activation_code FROM nexusai_site_codes WHERE site_id=%s", (row[0],))
                        code_row = cur.fetchone()
                        conn.commit()
                        return str(row[0]), str(code_row[0]) if code_row else ""
                    if row[1] != "UNUSED":
                        raise HTTPException(status_code=409, detail="This QR code is currently being activated. Please scan it again.")
                    site_id = _new_site_id()
                    activation_code = _new_activation_code()
                    cur.execute("INSERT INTO nexusai_site_codes(activation_code,site_id,created_at,last_used_at) VALUES(%s,%s,%s,%s)", (activation_code,site_id,now,now))
                    cur.execute("INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json) VALUES(%s,%s,%s,%s,%s,%s)", (site_id,"WAITING","", "", now, "[]"))
                    cur.execute("UPDATE nexusai_qr_tokens SET site_id=%s,status='ACTIVATED',last_used_at=%s WHERE qr_token=%s", (site_id,now,token))
                conn.commit()
                return site_id, activation_code
        with _sqlite() as conn:
            row = conn.execute("SELECT site_id,status FROM nexusai_qr_tokens WHERE qr_token=?", (token,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="NexusAI QR code not found.")
            if row[1] == "ACTIVATED" and row[0] and not str(row[0]).startswith("__POOL__:"):
                conn.execute("UPDATE nexusai_qr_tokens SET last_used_at=? WHERE qr_token=?", (now,token))
                code_row = conn.execute("SELECT activation_code FROM nexusai_site_codes WHERE site_id=?", (row[0],)).fetchone()
                conn.commit()
                return str(row[0]), str(code_row[0]) if code_row else ""
            if row[1] != "UNUSED":
                raise HTTPException(status_code=409, detail="This QR code is currently being activated. Please scan it again.")
            site_id = _new_site_id()
            activation_code = _new_activation_code()
            try:
                conn.execute("INSERT INTO nexusai_site_codes(activation_code,site_id,created_at,last_used_at) VALUES(?,?,?,?)", (activation_code,site_id,now,now))
                conn.execute("INSERT INTO nexusai_sites(site_id,status,agent_version,timestamp,received_at,cameras_json) VALUES(?,?,?,?,?,?)", (site_id,"WAITING","", "", now, "[]"))
                conn.execute("UPDATE nexusai_qr_tokens SET site_id=?,status='ACTIVATED',last_used_at=? WHERE qr_token=?", (site_id,now,token))
                conn.commit()
                return site_id, activation_code
            except sqlite3.IntegrityError:
                conn.rollback()
                raise HTTPException(status_code=409, detail="This QR code was activated at the same time by another device.")


def require_admin_key(value: str | None):
    configured = os.getenv("NEXUSAI_ADMIN_KEY", "").strip()
    if not configured or not value or not secrets.compare_digest(value.strip(), configured):
        raise HTTPException(status_code=401, detail="NexusAI admin access is required.")


def create_qr_pool(count: int) -> list[dict]:
    count = max(1, min(int(count), 500))
    created = []
    now = time.time()
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    for _ in range(count):
                        for _attempt in range(10):
                            token = _new_qr_token()
                            try:
                                cur.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at,last_used_at,status) VALUES(%s,%s,%s,%s,'UNUSED')", (token,_pool_placeholder(token),now,None))
                                created.append(token)
                                break
                            except Exception:
                                conn.rollback()
                        else:
                            raise HTTPException(status_code=500, detail="Could not generate a secure QR batch.")
                conn.commit()
        else:
            with _sqlite() as conn:
                for _ in range(count):
                    for _attempt in range(10):
                        token = _new_qr_token()
                        try:
                            conn.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at,last_used_at,status) VALUES(?,?,?,?,?)", (token,_pool_placeholder(token),now,None,"UNUSED"))
                            created.append(token)
                            break
                        except sqlite3.IntegrityError:
                            conn.rollback()
                    else:
                        raise HTTPException(status_code=500, detail="Could not generate a secure QR batch.")
                conn.commit()
    return [{"token":token,"url":"https://getnexusai.co.za/protect/?qr="+quote(token)} for token in created]


def list_qr_pool() -> list[dict]:
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT qr_token,site_id,status,created_at,last_used_at FROM nexusai_qr_tokens ORDER BY created_at DESC")
                    rows = cur.fetchall()
        else:
            with _sqlite() as conn:
                rows = conn.execute("SELECT qr_token,site_id,status,created_at,last_used_at FROM nexusai_qr_tokens ORDER BY created_at DESC").fetchall()
    return [{"token":row[0],"status":row[2] or "ACTIVATED","site_id":None if str(row[1]).startswith("__POOL__:") else row[1],"url":"https://getnexusai.co.za/protect/?qr="+quote(row[0]),"created_at":row[3],"last_used_at":row[4]} for row in rows]


def create_or_resolve_site(activation_code: str | None = None) -> tuple[str, str]:
    code = (activation_code or "").strip().upper()
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    if code:
                        cur.execute("SELECT activation_code,site_id FROM nexusai_site_codes WHERE activation_code=%s", (code,))
                        row = cur.fetchone()
                        if not row:
                            raise HTTPException(status_code=404, detail="Activation code not found.")
                        cur.execute("UPDATE nexusai_site_codes SET last_used_at=%s WHERE activation_code=%s", (time.time(), code))
                        conn.commit()
                        return row[1], row[0]
                    for _ in range(10):
                        site_id = _new_site_id()
                        new_code = _new_activation_code()
                        try:
                            cur.execute("INSERT INTO nexusai_site_codes (activation_code,site_id,created_at) VALUES(%s,%s,%s)", (new_code,site_id,time.time()))
                            cur.execute("INSERT INTO nexusai_sites (site_id,status,agent_version,timestamp,received_at,cameras_json) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (site_id) DO NOTHING", (site_id,"WAITING","", "", time.time(), "[]"))
                            conn.commit()
                            return site_id, new_code
                        except Exception:
                            conn.rollback()
                    raise HTTPException(status_code=500, detail="Could not create a NexusAI site. Please try again.")
        else:
            with _sqlite() as conn:
                if code:
                    row=conn.execute("SELECT activation_code,site_id FROM nexusai_site_codes WHERE activation_code=?", (code,)).fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="Activation code not found.")
                    conn.execute("UPDATE nexusai_site_codes SET last_used_at=? WHERE activation_code=?", (time.time(),code))
                    conn.commit()
                    return row[1], row[0]
                for _ in range(10):
                    site_id=_new_site_id()
                    new_code=_new_activation_code()
                    try:
                        conn.execute("INSERT INTO nexusai_site_codes (activation_code,site_id,created_at) VALUES(?,?,?)", (new_code,site_id,time.time()))
                        conn.execute("INSERT OR IGNORE INTO nexusai_sites (site_id,status,agent_version,timestamp,received_at,cameras_json) VALUES(?,?,?,?,?,?)", (site_id,"WAITING","", "", time.time(), "[]"))
                        conn.commit()
                        return site_id,new_code
                    except sqlite3.IntegrityError:
                        conn.rollback()
            raise HTTPException(status_code=500, detail="Could not create a NexusAI site. Please try again.")


@app.post("/api/portal/activate")
async def portal_activate(request: SiteActivationRequest, http_request: Request):
    allow_activation_attempt(http_request)
    site_id, activation_code = create_or_resolve_site(request.activation_code)
    qr_token = ensure_qr_token(site_id)
    return {"activated": True, "site_id": site_id, "activation_code": activation_code, "qr_url": f"https://getnexusai.co.za/protect/?qr={quote(qr_token)}"}


@app.get("/api/protect/session")
async def protect_session(qr: str = ""):
    info = qr_token_info(qr)
    if info["status"] != "ACTIVATED" or not info["site_id"] or str(info["site_id"]).startswith("__POOL__:"):
        return {"activated":False,"status":"UNUSED","site_id":None,"install_token":None,"edge_agent":"WAITING","cameras":[]}
    site_id = str(info["site_id"])
    site = EDGE_SITES.get(site_id) or load_site(site_id) or {"site_id":site_id,"status":"WAITING","cameras":[]}
    cameras = load_cameras(site_id)
    return {"activated":True,"status":"ACTIVATED","site_id":site_id,"install_token":create_install_token(site_id),"site_status":site.get("status","WAITING"),"edge_agent":"ONLINE" if site.get("received_at") and time.time()-float(site.get("received_at",0)) <= 90 else "WAITING","cameras":cameras}


class ActivateQrRequest(BaseModel):
    qr: str = Field(..., min_length=20, max_length=120)


@app.post("/api/protect/activate-qr")
async def activate_qr(request: ActivateQrRequest, http_request: Request):
    allow_activation_attempt(http_request)
    site_id, activation_code = claim_qr_for_new_site(request.qr)
    return {"activated":True,"site_id":site_id,"activation_code":activation_code,"qr_url":"https://getnexusai.co.za/protect/?qr="+quote(request.qr)}


class ProtectCamerasRequest(BaseModel):
    site_id: str = Field(..., min_length=8, max_length=100)
    install_token: str = Field(..., min_length=20, max_length=500)
    camera_ids: list[str] = Field(default_factory=list, max_length=2000)


@app.post("/api/protect/cameras")
async def protect_cameras(request: ProtectCamerasRequest):
    if not verify_install_token(request.site_id, request.install_token):
        raise HTTPException(status_code=403, detail="This NexusAI protection session is invalid or expired.")
    cameras = load_cameras(request.site_id)
    requested = {str(x) for x in request.camera_ids}
    selected = [c for c in cameras if str(c.get("camera_id")) in requested] if requested else cameras
    if not selected:
        raise HTTPException(status_code=400, detail="No cameras are currently available.")
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    for camera in selected:
                        cur.execute("UPDATE nexusai_cameras SET verified=TRUE,status='PROTECTED' WHERE site_id=%s AND camera_id=%s", (request.site_id, str(camera.get("camera_id"))))
                conn.commit()
        else:
            with _sqlite() as conn:
                for camera in selected:
                    conn.execute("UPDATE nexusai_cameras SET verified=1,status='PROTECTED' WHERE site_id=? AND camera_id=?", (request.site_id, str(camera.get("camera_id"))))
                conn.commit()
    return {"protected": True, "site_id": request.site_id, "protected_count": len(selected)}


@app.get("/api/protect/qr")
async def protect_qr(site_id: str = "", activation_code: str = ""):
    site_id = require_site_access(site_id, activation_code)
    token = ensure_qr_token(site_id)
    return {"site_id": site_id, "url": "https://getnexusai.co.za/protect/?qr=" + quote(token), "token": token}


@app.get("/api/admin/qr-pool")
async def admin_qr_pool(x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    return {"codes":list_qr_pool()}


class QrPoolGenerateRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=500)


@app.post("/api/admin/qr-pool")
async def admin_generate_qr_pool(request: QrPoolGenerateRequest, x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    return {"generated":create_qr_pool(request.count)}


class AdminCreateSiteRequest(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=200)
    store_address: str = Field(default="", max_length=300)
    contact_name: str = Field(default="", max_length=160)
    contact_phone: str = Field(default="", max_length=60)
    contact_email: str = Field(default="", max_length=200)
    camera_count: int = Field(default=0, ge=0, le=10000)

@app.post("/api/admin/sites")
async def admin_create_site(request: AdminCreateSiteRequest, x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    site_id, activation_code = create_or_resolve_site(None)
    save_customer({"site_id":site_id, **request.model_dump()})
    customer_token=create_customer_token(site_id)
    qr_token=ensure_qr_token(site_id)
    install_token=create_install_token(site_id)
    return {
        "site_id":site_id,
        "activation_code":activation_code,
        "qr_url":f"https://getnexusai.co.za/protect/?qr={quote(qr_token)}",
        "customer_portal_url":f"https://getnexusai.co.za/client/?token={quote(customer_token)}",
        "notification_app_url":f"https://getnexusai.co.za/app/?site_id={quote(site_id)}&install_token={quote(install_token)}",
        "installer_links": {
            "windows":f"https://getnexusai.co.za/downloads/security-box/windows.ps1?installer_token={quote(create_installer_token(site_id))}",
            "macos":f"https://getnexusai.co.za/downloads/security-box/macos.sh?installer_token={quote(create_installer_token(site_id))}"
        }
    }

@app.get("/api/admin/sites")
async def admin_list_sites(x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    site_ids=set()
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT site_id FROM nexusai_customers ORDER BY created_at DESC")
                    site_ids={r[0] for r in cur.fetchall()}
        else:
            with _sqlite() as conn:
                site_ids={r[0] for r in conn.execute("SELECT site_id FROM nexusai_customers ORDER BY created_at DESC").fetchall()}
    return {"sites":[admin_site_summary(x) for x in site_ids]}

@app.get("/api/admin/sites/{site_id}")
async def admin_get_site(site_id: str, x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    return admin_site_summary(validate_site_id(site_id))

@app.get("/api/admin/sites/{site_id}/links")
async def admin_site_links(site_id: str, x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    site_id=validate_site_id(site_id)
    customer=load_customer(site_id)
    if not customer:
        raise HTTPException(status_code=404, detail="NexusAI customer site not found.")
    customer_token=create_customer_token(site_id)
    install_token=create_install_token(site_id)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT activation_code FROM nexusai_site_codes WHERE site_id=%s", (site_id,))
                    row=cur.fetchone()
        else:
            with _sqlite() as conn:
                row=conn.execute("SELECT activation_code FROM nexusai_site_codes WHERE site_id=?", (site_id,)).fetchone()
    activation_code=str(row[0]) if row else ""
    return {
        "site_id":site_id,
        "activation_code":activation_code,
        "status":admin_site_summary(site_id),
        "customer_portal_url":f"https://getnexusai.co.za/client/?token={quote(customer_token)}",
        "notification_app_url":f"https://getnexusai.co.za/app/?site_id={quote(site_id)}&install_token={quote(install_token)}",
        "security_box_installer": {
            "windows":f"https://getnexusai.co.za/downloads/security-box/windows.ps1?installer_token={quote(create_installer_token(site_id))}",
            "macos":f"https://getnexusai.co.za/downloads/security-box/macos.sh?installer_token={quote(create_installer_token(site_id))}"
        }
    }

@app.get("/api/admin/sites/{site_id}/connect")
async def admin_connect_site(site_id: str, x_nexusai_admin_key: str | None = Header(default=None)):
    require_admin_key(x_nexusai_admin_key)
    site_id=validate_site_id(site_id)
    customer=load_customer(site_id)
    if not customer:
        raise HTTPException(status_code=404, detail="NexusAI customer site not found.")
    links=await admin_site_links(site_id, x_nexusai_admin_key)
    return {
        "site_id":site_id,
        "connected": links["status"]["edge_agent"]=="ONLINE",
        "status":links["status"],
        "activation_code":links["activation_code"],
        "next_step": "Security Box is online. Open the customer protection portal and connect the CCTV system." if links["status"]["edge_agent"]=="ONLINE" else "Install and open NexusAI Security Box at the customer site. It will connect outbound to NexusAI Cloud; do not expose the NVR to the public internet.",
        "links": {
            "customer_portal":links["customer_portal_url"],
            "notification_app":links["notification_app_url"],
            "windows_security_box":links["security_box_installer"]["windows"],
            "macos_security_box":links["security_box_installer"]["macos"]
        }
    }

@app.get("/api/customer/session")
async def customer_session(token: str = ""):
    site_id=require_customer_token(token)
    customer=load_customer(site_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer site not found.")
    site=EDGE_SITES.get(site_id) or load_site(site_id) or {"status":"WAITING","agent_version":"","received_at":0,"cameras":[]}
    cameras=load_cameras(site_id)
    install_token=create_install_token(site_id)
    return {**customer,
            "edge_agent":"ONLINE" if site.get("received_at") and time.time()-float(site.get("received_at",0))<=90 else "OFFLINE",
            "site_status":site.get("status","WAITING"),
            "agent_version":site.get("agent_version",""),
            "cameras":cameras,
            "notification_app_url":f"https://getnexusai.co.za/app/?site_id={quote(site_id)}&install_token={quote(install_token)}"}

@app.get("/api/customer/events")
async def customer_events(token: str = "", limit: int = 50):
    site_id=require_customer_token(token)
    return {"site_id":site_id,"events":load_events(site_id,max(1,min(int(limit),100)),None)}

@app.get("/api/customer/snapshots/{event_id}")
async def customer_snapshot(event_id: int, token: str = ""):
    site_id = require_customer_token(token)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT snapshot_mime,snapshot_filename,snapshot_data FROM nexusai_events WHERE id=%s AND site_id=%s",
                        (event_id, site_id),
                    )
                    row = cur.fetchone()
        else:
            with _sqlite() as conn:
                row = conn.execute(
                    "SELECT snapshot_mime,snapshot_filename,snapshot_data FROM nexusai_events WHERE id=? AND site_id=?",
                    (event_id, site_id),
                ).fetchone()
    if not row or not row[2]:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    return Response(
        content=bytes(row[2]),
        media_type=row[0] or "image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{row[1] or "snapshot.jpg"}"',
        },
    )

@app.post("/api/push/activate")
async def push_activate(request: SiteActivationRequest, http_request: Request):
    allow_activation_attempt(http_request)
    if not request.activation_code:
        raise HTTPException(status_code=400, detail="Enter the activation code shown on the NexusAI client portal.")
    site_id, activation_code = create_or_resolve_site(request.activation_code)
    token = create_install_token(site_id)
    return {"activated": True, "site_id": site_id, "activation_code": activation_code, "install_token": token}


@app.get("/api/push/install-link")
async def push_install_link(site_id: str = "", activation_code: str = "", x_nexusai_site_code: str | None = Header(default=None)):
    site_id = validate_site_id(site_id)
    require_site_access(site_id, activation_code or x_nexusai_site_code)
    if not site_id or len(site_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid site ID.")
    token = create_install_token(site_id)
    return {
        "site_id": site_id,
        "url": f"/app/?site_id={quote(site_id)}&install_token={quote(token)}",
        "expires_in": 86400,
    }


@app.get("/api/portal/snapshots/{event_id}")
async def portal_snapshot(event_id:int,site_id:str,access_code:str=""):
    require_site_access(site_id, access_code)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT snapshot_mime,snapshot_filename,snapshot_data FROM nexusai_events WHERE id=%s AND site_id=%s",(event_id,site_id))
                    row=cur.fetchone()
        else:
            with _sqlite() as conn:
                row=conn.execute("SELECT snapshot_mime,snapshot_filename,snapshot_data FROM nexusai_events WHERE id=? AND site_id=?",(event_id,site_id)).fetchone()
    if not row or not row[2]: raise HTTPException(status_code=404,detail="Snapshot not found.")
    from fastapi.responses import Response
    return Response(content=bytes(row[2]),media_type=row[0] or "image/jpeg",headers={"Cache-Control":"private, max-age=300","Content-Disposition":f'inline; filename="{row[1] or "snapshot.jpg"}"'})

@app.get("/api/portal/cameras")
async def portal_cameras(site_id:str="", x_nexusai_site_code: str | None = Header(default=None)):
    site_id = require_site_access(site_id, x_nexusai_site_code)
    return {"site_id":site_id,"cameras":load_cameras(site_id)}

@app.get("/api/push/config")
async def push_config():
    return {
        "configured": push_configured(),
        "public_key": os.getenv("NEXUSAI_VAPID_PUBLIC_KEY", "").strip(),
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
    if not verify_install_token(request.site_id, request.install_token):
        raise HTTPException(status_code=403, detail="This NexusAI app installation is invalid or expired.")
    endpoint = str(request.subscription.get("endpoint", "")).strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="Push endpoint required.")
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM nexusai_push_subscriptions WHERE endpoint=%s AND site_id=%s", (endpoint, request.site_id))
                conn.commit()
        else:
            with _sqlite() as conn:
                conn.execute("DELETE FROM nexusai_push_subscriptions WHERE endpoint=? AND site_id=?", (endpoint, request.site_id))
                conn.commit()
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
        "version": "3.3.0",
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
        "api_version": "3.2.0",
    }


def _new_installer_token() -> str:
    return secrets.token_urlsafe(36).replace("-", "").replace("_", "")

def _installer_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def create_installer_token(site_id: str, ttl_seconds: int = 900) -> str:
    # Installer links are signed, short-lived tokens. They do not require a
    # separate database write, so generating an installer cannot fail because
    # the installer-token table is unavailable.
    site_id = validate_site_id(site_id)
    now = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{site_id}.{now}.{nonce}"
    signature = hmac.new(app_link_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}.{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def verify_installer_token(token: str, max_age: int = 900) -> str:
    token = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,220}", token):
        raise HTTPException(status_code=400, detail="Invalid NexusAI installer token.")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        site_id, issued, nonce, signature = raw.split(".", 3)
        issued_int = int(issued)
        if abs(int(time.time()) - issued_int) > max_age:
            raise HTTPException(status_code=403, detail="This NexusAI installer link has expired. Please scan the QR code again.")
        payload = f"{site_id}.{issued}.{nonce}"
        expected = hmac.new(app_link_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="This NexusAI installer link is invalid.")
        return validate_site_id(site_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid NexusAI installer token.")

def consume_installer_token(token: str) -> str:
    # Kept as a separate function because the Security Box bootstrap endpoint
    # consumes the installer credential. The signed token is stateless.
    return verify_installer_token(token, max_age=900)

@app.get("/api/edge/installer-link-by-qr")
async def edge_installer_link_by_qr(qr: str = ""):
    try:
        site_id = resolve_qr_token(qr)
        token = create_installer_token(site_id)
    except HTTPException:
        raise
    except Exception:
        logging.exception("NexusAI Security Box installer-link generation failed.")
        raise HTTPException(status_code=503, detail="NexusAI Cloud could not prepare the Security Box installer. Please try again.")
    encoded = quote(token)
    return {
        "site_id": site_id,
        "expires_in": 900,
        "windows": f"/downloads/security-box/windows.exe?installer_token={encoded}",
        "macos_arm64": f"/downloads/security-box/macos.pkg?installer_token={encoded}&arch=arm64",
        "macos_intel": f"/downloads/security-box/macos.pkg?installer_token={encoded}&arch=x86_64",
    }

@app.get("/api/edge/installer-link")
async def edge_installer_link(site_id: str, activation_code: str):
    site_id = require_site_access(site_id, activation_code)
    token = create_installer_token(site_id)
    encoded = quote(token)
    return {
        "site_id": site_id,
        "expires_in": 900,
        "windows": f"/downloads/security-box/windows.exe?installer_token={encoded}",
        "macos_arm64": f"/downloads/security-box/macos.pkg?installer_token={encoded}&arch=arm64",
        "macos_intel": f"/downloads/security-box/macos.pkg?installer_token={encoded}&arch=x86_64",
    }

@app.post("/api/edge/bootstrap")
async def edge_bootstrap(request: dict):
    token = str(request.get("installer_token", "")).strip()
    site_id = consume_installer_token(token)
    return {
        "site_id": site_id,
        "edge_token": site_edge_token(site_id),
        "api_url": "https://getnexusai.co.za",
        "expires_in": 86400,
    }

@app.get("/api/edge/provision")
async def edge_provision(site_id: str, activation_code: str):
    site_id = require_site_access(site_id, activation_code)
    return {"site_id": site_id, "edge_token": site_edge_token(site_id), "api_url": "https://getnexusai.co.za"}


@app.post("/api/edge/heartbeat")
async def edge_heartbeat(
    heartbeat: EdgeHeartbeat,
    x_nexusai_edge_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    require_edge_token(x_nexusai_edge_token, authorization, heartbeat.site_id)
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
    require_edge_token(x_nexusai_edge_token, authorization, verification.site_id)
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
    require_edge_token(x_nexusai_edge_token, authorization, event.site_id)
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
async def portal_status(site_id: str = "", x_nexusai_site_code: str | None = Header(default=None)):
    site_id = require_site_access(site_id, x_nexusai_site_code)
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
async def portal_events(site_id: str = "", limit: int = 50, x_nexusai_site_code: str | None = Header(default=None)):
    site_id = require_site_access(site_id, x_nexusai_site_code)
    limit = max(1, min(int(limit), 100))
    return {"site_id": site_id, "events": load_events(site_id, limit, x_nexusai_site_code)}


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
        "cameras": load_cameras(site_id),
    }


@app.get("/", include_in_schema=False)
async def nexusai_root():
    return FileResponse(PORTAL_DIR / "qr-admin.html", media_type="text/html", headers={"Cache-Control":"no-store"})

# Keep the portal mounted last so API routes stay reachable.
app.mount("/portal", StaticFiles(directory=PORTAL_DIR, html=True), name="portal")