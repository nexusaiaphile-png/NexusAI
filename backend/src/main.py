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
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
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
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://getnexusai.co.za", "https://www.getnexusai.co.za"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


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
                        last_used_at DOUBLE PRECISION
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_qr_site ON nexusai_qr_tokens(site_id)")
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
                    last_used_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_nexusai_qr_site ON nexusai_qr_tokens(site_id);
            """)
            conn.commit()


@app.on_event("startup")
def startup():
    init_db()
    if database_url():
        import psycopg
        with psycopg.connect(database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_mime TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_filename TEXT")
                cur.execute("ALTER TABLE nexusai_events ADD COLUMN IF NOT EXISTS snapshot_data BYTEA")
            conn.commit()
    else:
        with _sqlite() as conn:
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


@app.get("/portal", include_in_schema=False)
@app.get("/portal/", include_in_schema=False)
async def client_portal():
    return FileResponse(PORTAL_DIR / "index.html")

@app.get("/protect", include_in_schema=False)
@app.get("/protect/", include_in_schema=False)
async def simple_protect():
    return FileResponse(PORTAL_DIR / "protect.html")


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
    return secrets.token_urlsafe(24).replace("-", "").replace("_", "")


def ensure_qr_token(site_id: str) -> str:
    site_id = validate_site_id(site_id)
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT qr_token FROM nexusai_qr_tokens WHERE site_id=%s", (site_id,))
                    row = cur.fetchone()
                    if row:
                        return row[0]
                    token = _new_qr_token()
                    cur.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at) VALUES(%s,%s,%s)", (token, site_id, time.time()))
                conn.commit()
                return token
        with _sqlite() as conn:
            row = conn.execute("SELECT qr_token FROM nexusai_qr_tokens WHERE site_id=?", (site_id,)).fetchone()
            if row:
                return row[0]
            for _ in range(10):
                token = _new_qr_token()
                try:
                    conn.execute("INSERT INTO nexusai_qr_tokens(qr_token,site_id,created_at) VALUES(?,?,?)", (token, site_id, time.time()))
                    conn.commit()
                    return token
                except sqlite3.IntegrityError:
                    conn.rollback()
            raise HTTPException(status_code=500, detail="Could not create NexusAI QR token.")


def resolve_qr_token(qr_token: str) -> str:
    token = str(qr_token or "").strip()
    if len(token) < 20 or len(token) > 100 or not re.fullmatch(r"[A-Za-z0-9]+", token):
        raise HTTPException(status_code=400, detail="Invalid NexusAI QR code.")
    with DB_LOCK:
        if database_url():
            import psycopg
            with psycopg.connect(database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT site_id FROM nexusai_qr_tokens WHERE qr_token=%s", (token,))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="NexusAI site QR code not found.")
                    cur.execute("UPDATE nexusai_qr_tokens SET last_used_at=%s WHERE qr_token=%s", (time.time(), token))
                conn.commit()
                return row[0]
        with _sqlite() as conn:
            row = conn.execute("SELECT site_id FROM nexusai_qr_tokens WHERE qr_token=?", (token,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="NexusAI site QR code not found.")
            conn.execute("UPDATE nexusai_qr_tokens SET last_used_at=? WHERE qr_token=?", (time.time(), token))
            conn.commit()
            return row[0]


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
    return {"activated": True, "site_id": site_id, "activation_code": activation_code}


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
        "version": "3.1.0",
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
        "api_version": "3.1.0",
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


# Keep the portal mounted last so API routes stay reachable.
app.mount("/portal", StaticFiles(directory=PORTAL_DIR, html=True), name="portal")
