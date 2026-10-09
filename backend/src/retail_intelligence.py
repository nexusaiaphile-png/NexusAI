"""NexusAI Retail Intelligence v1: durable POS/camera correlation API."""
import json, os, sqlite3, time, uuid
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("NEXUSAI_DB_PATH", str(Path(__file__).resolve().parents[2] / "nexusai.db")))
WINDOW = max(1, min(30, int(os.getenv("NEXUSAI_RETAIL_MATCH_WINDOW_SECONDS", "5"))))

class Item(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    item_name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(default=1, gt=0, le=10000)
    unit_price: float | None = Field(default=None, ge=0)

class Transaction(BaseModel):
    site_id: str = Field(min_length=3, max_length=100)
    lane_id: str = Field(min_length=1, max_length=100)
    transaction_id: str = Field(min_length=1, max_length=150)
    timestamp: datetime
    items: list[Item] = Field(min_length=1, max_length=500)

class CameraEvent(BaseModel):
    site_id: str = Field(min_length=3, max_length=100)
    lane_id: str = Field(min_length=1, max_length=100)
    camera_id: str = Field(min_length=1, max_length=150)
    event_id: str = Field(min_length=1, max_length=200)
    timestamp: datetime
    snapshot_url: str | None = Field(default=None, max_length=2000)
    event_type: str = Field(default="CHECKOUT_ACTIVITY", max_length=120)

class VisionResult(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    transaction_id: str = Field(min_length=1, max_length=150)
    observed_items: list[dict] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)
    model_name: str = Field(default="vision-provider", max_length=100)

def _db():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        import psycopg
        return psycopg.connect(url), True
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c, False

def _q(c, pg, sql, args=()):
    return c.execute(sql.replace("?", "%s") if pg else sql, args)

def _init():
    c, pg = _db()
    try:
        for sql in [
          "CREATE TABLE IF NOT EXISTS retail_pos_transactions (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, lane_id TEXT NOT NULL, transaction_id TEXT NOT NULL UNIQUE, event_time TEXT NOT NULL, items_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
          "CREATE TABLE IF NOT EXISTS retail_camera_events (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, lane_id TEXT NOT NULL, camera_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, event_time TEXT NOT NULL, snapshot_url TEXT, event_type TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
          "CREATE TABLE IF NOT EXISTS retail_vision_results (id TEXT PRIMARY KEY, event_id TEXT NOT NULL, transaction_id TEXT NOT NULL, observed_json TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL, model_name TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, UNIQUE(event_id,transaction_id))",
          "CREATE TABLE IF NOT EXISTS retail_incidents (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, lane_id TEXT NOT NULL, transaction_id TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL)"
        ]: _q(c, pg, sql)
        _q(c, pg, "CREATE INDEX IF NOT EXISTS idx_retail_pos_lane_time ON retail_pos_transactions(site_id,lane_id,event_time)")
        _q(c, pg, "CREATE INDEX IF NOT EXISTS idx_retail_cam_lane_time ON retail_camera_events(site_id,lane_id,event_time)")
        c.commit()
    finally: c.close()

def _guard(key, env):
    expected = os.getenv(env, "").strip()
    if not expected: raise HTTPException(503, f"{env} is not configured.")
    if not key or key != expected: raise HTTPException(401, "Invalid integration token.")

def _utc(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def ingest_hpp_event(event: dict) -> bool:
    """Bridge normalized HPP alarms into the retail camera-event table.

    Configure NEXUSAI_RETAIL_CAMERA_LANE_MAP as JSON, with keys of
    "nexusai_site_id|camera_id" and values of the matching POS lane ID.
    Events without an explicit mapping are intentionally ignored.
    """
    raw_map = os.getenv("NEXUSAI_RETAIL_CAMERA_LANE_MAP", "").strip()
    if not raw_map:
        return False
    try:
        lane_map = json.loads(raw_map)
    except Exception as exc:
        raise RuntimeError("NEXUSAI_RETAIL_CAMERA_LANE_MAP must be valid JSON.") from exc
    if not isinstance(lane_map, dict):
        raise RuntimeError("NEXUSAI_RETAIL_CAMERA_LANE_MAP must be a JSON object.")

    site_id = str(event.get("site_id") or "").strip()
    camera_id = str(event.get("camera_id") or "").strip()
    serial = str(event.get("hpp_device_serial") or "").strip()
    channel = str(event.get("hpp_channel") or "").strip()
    lane_id = (lane_map.get(f"{site_id}|{camera_id}")
               or lane_map.get(f"{site_id}|{serial}|{channel}")
               or lane_map.get(f"{site_id}|{serial}")
               or lane_map.get(camera_id))
    if not lane_id or not site_id or not camera_id:
        return False

    timestamp = str(event.get("timestamp") or datetime.now(timezone.utc).isoformat())
    event_key = str(event.get("event_id") or "")
    if not event_key:
        import hashlib
        stable = "|".join([site_id, camera_id, timestamp, str(event.get("event") or "")])
        event_key = "hpp-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:48]
    hpp_data = event.get("hpp_data") if isinstance(event.get("hpp_data"), dict) else {}
    picture_url = (event.get("snapshot_url") or hpp_data.get("pictureUrl")
                   or hpp_data.get("picture") or hpp_data.get("filePath") or hpp_data.get("url"))
    c, pg = _db()
    try:
        _q(c, pg, "INSERT INTO retail_camera_events VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
           ("hpp-" + uuid.uuid4().hex, site_id, str(lane_id), camera_id, event_key,
            _utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))),
            str(picture_url) if picture_url else None,
            str(event.get("event") or "HIKVISION_ALARM"), time.time()))
        c.commit()
        return True
    finally:
        c.close()

def install(app: FastAPI):
    _init()
    @app.get("/api/retail/status")
    async def status(x_nexusai_admin_key: str | None = Header(default=None)):
        _guard(x_nexusai_admin_key, "NEXUSAI_ADMIN_KEY")
        return {"product":"NexusAI Retail Intelligence","version":"0.1.0","match_window_seconds":WINDOW,
                "pos_ingestion_enabled":bool(os.getenv("NEXUSAI_POS_INGEST_TOKEN","")),
                "vision":"Requires a real vision provider; no mocked prediction is used."}

    @app.post("/api/retail/pos/transactions")
    async def pos(payload: Transaction, x_nexusai_pos_token: str | None = Header(default=None)):
        _guard(x_nexusai_pos_token, "NEXUSAI_POS_INGEST_TOKEN")
        c, pg = _db()
        try:
            _q(c,pg,"INSERT INTO retail_pos_transactions VALUES (?,?,?,?,?,?,?) ON CONFLICT(transaction_id) DO NOTHING",
               ("pos-"+uuid.uuid4().hex,payload.site_id,payload.lane_id,payload.transaction_id,_utc(payload.timestamp),
                json.dumps([x.model_dump() for x in payload.items]),time.time()))
            c.commit()
        except Exception:
            c.rollback()
            raise HTTPException(409,"Transaction could not be stored; check transaction_id and database.")
        finally: c.close()
        return {"accepted":True,"transaction_id":payload.transaction_id,"status":"stored_for_correlation"}

    @app.post("/api/retail/camera-events")
    async def camera(payload: CameraEvent, x_nexusai_pos_token: str | None = Header(default=None)):
        _guard(x_nexusai_pos_token, "NEXUSAI_POS_INGEST_TOKEN")
        ts = _utc(payload.timestamp)
        c, pg = _db()
        try:
            _q(c,pg,"INSERT INTO retail_camera_events VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
               ("cam-"+uuid.uuid4().hex,payload.site_id,payload.lane_id,payload.camera_id,payload.event_id,ts,
                payload.snapshot_url,payload.event_type,time.time()))
            c.commit()
            rows = _q(c,pg,"SELECT transaction_id,event_time,items_json FROM retail_pos_transactions WHERE site_id=? AND lane_id=?",
                      (payload.site_id,payload.lane_id)).fetchall()
        finally: c.close()
        target=datetime.fromisoformat(ts)
        candidates=[]
        for row in rows:
            txid, stamp, items = row
            diff=abs((target-datetime.fromisoformat(stamp)).total_seconds())
            if diff <= WINDOW: candidates.append({"transaction_id":txid,"time_difference_seconds":round(diff,3),"items":json.loads(items)})
        candidates.sort(key=lambda x:x["time_difference_seconds"])
        return {"accepted":True,"event_id":payload.event_id,"candidate_transactions":candidates[:5],
                "match_window_seconds":WINDOW,"note":"Time/lane match is not proof of a product discrepancy."}

    @app.post("/api/retail/vision-results")
    async def vision(payload: VisionResult, x_nexusai_pos_token: str | None = Header(default=None)):
        _guard(x_nexusai_pos_token,"NEXUSAI_POS_INGEST_TOKEN")
        if payload.confidence < float(os.getenv("NEXUSAI_RETAIL_MIN_CONFIDENCE","0.85")):
            return {"accepted":False,"reason":"below_confidence_threshold"}
        c,pg=_db()
        try:
            cam=_q(c,pg,"SELECT site_id,lane_id,event_time FROM retail_camera_events WHERE event_id=?",(payload.event_id,)).fetchone()
            pos=_q(c,pg,"SELECT site_id,lane_id,event_time,items_json FROM retail_pos_transactions WHERE transaction_id=?",(payload.transaction_id,)).fetchone()
            if not cam or not pos: raise HTTPException(404,"Camera event or transaction not found.")
            if cam[0]!=pos[0] or cam[1]!=pos[1]: raise HTTPException(409,"Site/lane mismatch.")
            if abs((datetime.fromisoformat(cam[2])-datetime.fromisoformat(pos[2])).total_seconds()) > WINDOW:
                raise HTTPException(422,"Event is outside the configured time window.")
            _q(c,pg,"INSERT INTO retail_vision_results VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id,transaction_id) DO NOTHING",
               ("vis-"+uuid.uuid4().hex,payload.event_id,payload.transaction_id,json.dumps(payload.observed_items),payload.confidence,payload.model_name,time.time()))
            c.commit()
            receipt=json.loads(pos[3])
        finally: c.close()
        seen={str(i.get("sku","")).strip().casefold() for i in payload.observed_items if i.get("sku")}
        expected={str(i.get("sku","")).strip().casefold() for i in receipt if i.get("sku")}
        if seen and expected and seen.isdisjoint(expected):
            incident="incident-"+uuid.uuid4().hex; now=time.time()
            evidence={"receipt_items":receipt,"observed_items":payload.observed_items,"confidence":payload.confidence,"model":payload.model_name}
            c,pg=_db()
            try:
                _q(c,pg,"INSERT INTO retail_incidents VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (incident,cam[0],cam[1],payload.transaction_id,payload.event_id,"OPEN",
                    "Possible SKU discrepancy — human review required.",json.dumps(evidence),now,now)); c.commit()
            finally: c.close()
            return {"accepted":True,"incident_created":True,"incident_id":incident,"status":"OPEN",
                    "warning":"Investigation lead only; not a confirmed fraud finding."}
        return {"accepted":True,"incident_created":False,"result":"no_comparable_sku_or_match"}

    @app.get("/api/retail/incidents")
    async def incidents(status: str = "OPEN", limit: int = 50, x_nexusai_admin_key: str | None = Header(default=None)):
        _guard(x_nexusai_admin_key,"NEXUSAI_ADMIN_KEY")
        c,pg=_db()
        try:
            rows=_q(c,pg,"SELECT id,site_id,lane_id,transaction_id,event_id,status,summary,evidence_json,created_at,updated_at FROM retail_incidents WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status,max(1,min(limit,200)))).fetchall()
            return {"incidents":[{"id":r[0],"site_id":r[1],"lane_id":r[2],"transaction_id":r[3],"event_id":r[4],
                "status":r[5],"summary":r[6],"evidence":json.loads(r[7]),"created_at":r[8],"updated_at":r[9]} for r in rows]}
        finally: c.close()

    @app.post("/api/retail/incidents/{incident_id}/status")
    async def update_incident(incident_id: str, payload: dict, x_nexusai_admin_key: str | None = Header(default=None)):
        _guard(x_nexusai_admin_key,"NEXUSAI_ADMIN_KEY")
        status=str(payload.get("status","")).upper()
        if status not in {"OPEN","REVIEWING","RESOLVED","FALSE_POSITIVE"}:
            raise HTTPException(422,"Invalid incident status.")
        c,pg=_db()
        try:
            cur=_q(c,pg,"UPDATE retail_incidents SET status=?,updated_at=? WHERE id=?",(status,time.time(),incident_id)); c.commit()
            if cur.rowcount==0: raise HTTPException(404,"Incident not found.")
            return {"updated":True,"id":incident_id,"status":status}
        finally: c.close()
