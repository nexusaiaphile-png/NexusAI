"""NexusAI managed deployment pairing control plane."""
from __future__ import annotations
import os, secrets, sqlite3, time, hmac, hashlib
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from credential_vault import seal, open_sealed

router = APIRouter(prefix="/api")

def _db_url(): return os.getenv("DATABASE_URL","").strip()
def _path():
    from pathlib import Path
    return Path(os.getenv("NEXUSAI_DB_PATH","nexusai.db"))
def _conn():
    if _db_url():
        import psycopg
        return psycopg.connect(_db_url())
    c=sqlite3.connect(_path(),timeout=15); c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000"); return c
def _schema():
    c=_conn()
    try:
        x=c.cursor()
        if _db_url():
            x.execute("""CREATE TABLE IF NOT EXISTS nexusai_deployments (
                site_id TEXT PRIMARY KEY, device_id TEXT, paired BOOLEAN NOT NULL DEFAULT FALSE,
                paired_at DOUBLE PRECISION, last_seen DOUBLE PRECISION)""")
            x.execute("""CREATE TABLE IF NOT EXISTS nexusai_deployment_commands (
                id BIGSERIAL PRIMARY KEY, site_id TEXT NOT NULL, command_type TEXT NOT NULL,
                payload_json TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
                created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                delivered_at DOUBLE PRECISION, completed_at DOUBLE PRECISION, failure_reason TEXT)""")
            x.execute("CREATE INDEX IF NOT EXISTS idx_nexusai_deploy_commands ON nexusai_deployment_commands(site_id,status)")
        else:
            x.executescript("""CREATE TABLE IF NOT EXISTS nexusai_deployments (
                site_id TEXT PRIMARY KEY, device_id TEXT, paired INTEGER NOT NULL DEFAULT 0,
                paired_at REAL, last_seen REAL);
            CREATE TABLE IF NOT EXISTS nexusai_deployment_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, command_type TEXT NOT NULL,
                payload_json TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                delivered_at REAL, completed_at REAL, failure_reason TEXT);
            CREATE INDEX IF NOT EXISTS idx_nexusai_deploy_commands ON nexusai_deployment_commands(site_id,status);""")
        c.commit()
    finally: c.close()
def _admin(v):
    k=os.getenv("NEXUSAI_ADMIN_KEY","").strip()
    if not k or not v or not secrets.compare_digest(k,v.strip()):
        raise HTTPException(401,"NexusAI admin access is required.")
def _edge(site_id,v):
    value = (v or "").strip()
    if not value:
        raise HTTPException(401,"NexusAI Security Box authentication required.")
    legacy = os.getenv("NEXUSAI_EDGE_TOKEN","").strip()
    if legacy and secrets.compare_digest(legacy,value):
        return
    k=os.getenv("NEXUSAI_APP_LINK_SECRET","").strip()
    if k:
        expected=hmac.new(k.encode(),("nexusai-edge:"+site_id).encode(),hashlib.sha256).hexdigest()
        if secrets.compare_digest(expected,value):
            return
    raise HTTPException(401,"NexusAI Security Box authentication failed.")
def _deployment(site_id):
    _schema(); c=_conn()
    try:
        q="SELECT site_id,device_id,paired,paired_at,last_seen FROM nexusai_deployments WHERE site_id=%s" if _db_url() else "SELECT site_id,device_id,paired,paired_at,last_seen FROM nexusai_deployments WHERE site_id=?"
        cur=c.cursor(); cur.execute(q,(site_id,)); r=cur.fetchone()
        if not r: return {"site_id":site_id,"device_id":None,"paired":False,"paired_at":None,"last_seen":None}
        return {"site_id":r[0],"device_id":r[1],"paired":bool(r[2]),"paired_at":r[3],"last_seen":r[4]}
    finally: c.close()
def _register_device(site_id,device_id):
    _schema(); now=time.time(); c=_conn()
    try:
        cur=c.cursor()
        if _db_url():
            cur.execute("""INSERT INTO nexusai_deployments(site_id,device_id,paired,last_seen) VALUES(%s,%s,FALSE,%s)
                ON CONFLICT(site_id) DO UPDATE SET device_id=COALESCE(nexusai_deployments.device_id,EXCLUDED.device_id),last_seen=EXCLUDED.last_seen""",(site_id,device_id,now))
        else:
            cur.execute("""INSERT INTO nexusai_deployments(site_id,device_id,paired,last_seen) VALUES(?,?,0,?)
                ON CONFLICT(site_id) DO UPDATE SET device_id=COALESCE(nexusai_deployments.device_id,excluded.device_id),last_seen=excluded.last_seen""",(site_id,device_id,now))
        c.commit()
    finally: c.close()
def _set_paired(site_id,device_id=None):
    _schema(); now=time.time(); c=_conn()
    try:
        cur=c.cursor()
        if _db_url():
            cur.execute("""INSERT INTO nexusai_deployments(site_id,device_id,paired,paired_at,last_seen)
                VALUES(%s,%s,TRUE,%s,%s)
                ON CONFLICT(site_id) DO UPDATE SET device_id=COALESCE(EXCLUDED.device_id,nexusai_deployments.device_id),
                paired=TRUE,paired_at=EXCLUDED.paired_at,last_seen=EXCLUDED.last_seen""",(site_id,device_id,now,now))
        else:
            cur.execute("""INSERT INTO nexusai_deployments(site_id,device_id,paired,paired_at,last_seen)
                VALUES(?,?,1,?,?) ON CONFLICT(site_id) DO UPDATE SET
                device_id=COALESCE(excluded.device_id,nexusai_deployments.device_id),
                paired=1,paired_at=excluded.paired_at,last_seen=excluded.last_seen""",(site_id,device_id,now,now))
        c.commit()
    finally: c.close()
def _commands(site_id):
    _schema(); now=time.time(); c=_conn()
    try:
        cur=c.cursor()
        q=("SELECT id,command_type,payload_json,status,created_at,expires_at FROM nexusai_deployment_commands "
           "WHERE site_id=%s AND status IN ('PENDING','DELIVERED') AND expires_at>%s ORDER BY id") if _db_url() else (
           "SELECT id,command_type,payload_json,status,created_at,expires_at FROM nexusai_deployment_commands "
           "WHERE site_id=? AND status IN ('PENDING','DELIVERED') AND expires_at>? ORDER BY id")
        cur.execute(q,(site_id,now))
        return [dict(zip(["id","command_type","payload_json","status","created_at","expires_at"],r)) for r in cur.fetchall()]
    finally: c.close()
def _new_command(site_id,kind,payload=None,ttl=600):
    _schema(); now=time.time(); c=_conn()
    try:
        cur=c.cursor(); body=seal(payload or {}) if payload is not None else None
        if _db_url():
            cur.execute("""INSERT INTO nexusai_deployment_commands(site_id,command_type,payload_json,status,created_at,expires_at)
                VALUES(%s,%s,%s,'PENDING',%s,%s) RETURNING id""",(site_id,kind,body,now,now+ttl)); cid=cur.fetchone()[0]
        else:
            cur.execute("""INSERT INTO nexusai_deployment_commands(site_id,command_type,payload_json,status,created_at,expires_at)
                VALUES(?,?,?,'PENDING',?,?)""",(site_id,kind,body,now,now+ttl)); cid=cur.lastrowid
        c.commit(); return int(cid)
    finally: c.close()
def _finish(cid,site_id,status):
    _schema(); now=time.time(); c=_conn()
    try:
        cur=c.cursor()
        q="UPDATE nexusai_deployment_commands SET status=%s,completed_at=%s WHERE id=%s AND site_id=%s" if _db_url() else "UPDATE nexusai_deployment_commands SET status=?,completed_at=? WHERE id=? AND site_id=?"
        cur.execute(q,(status,now,cid,site_id))
        # Credential payloads are one-time secrets: remove ciphertext after acknowledgement.
        if status in ("COMPLETED","FAILED","DECLINED","CANCELLED"):
            dq="UPDATE nexusai_deployment_commands SET payload_json=NULL WHERE id=%s AND site_id=%s" if _db_url() else "UPDATE nexusai_deployment_commands SET payload_json=NULL WHERE id=? AND site_id=?"
            cur.execute(dq,(cid,site_id))
        c.commit()
    finally: c.close()

class PairRequest(BaseModel):
    device_id: str | None = Field(default=None,max_length=200)

class CredentialsRequest(BaseModel):
    username: str = Field(...,min_length=1,max_length=128)
    password: str = Field(...,min_length=1,max_length=512)
    location: str = Field(default="Security Site",max_length=200)
    nvr_ip: str | None = Field(default=None,max_length=64)
    nvr_port: int = Field(default=80,ge=1,le=65535)

@router.post("/admin/deployments/{site_id}/pair")
async def request_pair(site_id:str, body:PairRequest, x_nexusai_admin_key:str|None=Header(default=None)):
    _admin(x_nexusai_admin_key)
    d=_deployment(site_id)
    if d["paired"]: return {"site_id":site_id,"status":"PAIRED","device_id":d["device_id"]}
    cid=_new_command(site_id,"PAIR_REQUEST",{"device_id":body.device_id},600)
    return {"site_id":site_id,"command_id":cid,"status":"PAIRING_REQUESTED","expires_in":600}

@router.get("/admin/deployments/{site_id}")
async def deployment_status(site_id:str,x_nexusai_admin_key:str|None=Header(default=None)):
    _admin(x_nexusai_admin_key); d=_deployment(site_id)
    return {"deployment":d,"pending_commands":[{"id":x["id"],"type":x["command_type"],"status":x["status"],"expires_at":x["expires_at"]} for x in _commands(site_id)]}

@router.get("/edge/commands")
async def edge_commands(site_id:str,device_id:str="",x_nexusai_edge_token:str|None=Header(default=None,alias="X-NexusAI-Edge-Token")):
    _edge(site_id,x_nexusai_edge_token)
    if device_id:
        _set_paired(site_id,device_id=device_id) if _deployment(site_id)["paired"] else _register_device(site_id,device_id)
    out=[]
    for x in _commands(site_id):
        payload=open_sealed(x["payload_json"]) if x["payload_json"] else {}
        if x["command_type"]=="PAIR_REQUEST" and payload.get("device_id") and payload.get("device_id") != device_id:
            continue
        out.append({"id":x["id"],"type":x["command_type"],"payload":payload,"expires_at":x["expires_at"]})
    return {"site_id":site_id,"commands":out}

@router.post("/edge/commands/{command_id}/confirm")
async def confirm_pair(command_id:int,site_id:str,device_id:str="",x_nexusai_edge_token:str|None=Header(default=None,alias="X-NexusAI-Edge-Token")):
    _edge(site_id,x_nexusai_edge_token)
    commands=_commands(site_id); match=next((x for x in commands if x["id"]==command_id and x["command_type"]=="PAIR_REQUEST"),None)
    if not match: raise HTTPException(404,"Pairing request not found or expired.")
    expected_device=_deployment(site_id)["device_id"]
    if expected_device and device_id and not secrets.compare_digest(expected_device,device_id): raise HTTPException(403,"Security Box identity mismatch.")
    _finish(command_id,site_id,"COMPLETED"); _set_paired(site_id,device_id=device_id or expected_device)
    return {"confirmed":True,"site_id":site_id,"status":"PAIRED"}

@router.post("/edge/commands/{command_id}/decline")
async def decline_pair(command_id:int,site_id:str,x_nexusai_edge_token:str|None=Header(default=None,alias="X-NexusAI-Edge-Token")):
    _edge(site_id,x_nexusai_edge_token); _finish(command_id,site_id,"DECLINED")
    return {"declined":True,"site_id":site_id}

@router.post("/admin/deployments/{site_id}/credentials")
async def queue_credentials(site_id:str, body:CredentialsRequest, x_nexusai_admin_key:str|None=Header(default=None)):
    _admin(x_nexusai_admin_key)
    if not _deployment(site_id)["paired"]:
        raise HTTPException(409,"Security Box must be paired before CCTV credentials can be delivered.")
    cid=_new_command(site_id,"SET_CCTV_CREDENTIALS",body.model_dump(),900)
    return {"site_id":site_id,"command_id":cid,"status":"QUEUED","expires_in":900}

@router.post("/edge/commands/{command_id}/complete")
async def complete_command(command_id:int, site_id:str, success:bool=False, error:str="", x_nexusai_edge_token:str|None=Header(default=None,alias="X-NexusAI-Edge-Token")):
    _edge(site_id,x_nexusai_edge_token)
    _finish(command_id,site_id,"COMPLETED" if success else "FAILED")
    return {"completed":True,"site_id":site_id,"status":"COMPLETED" if success else "FAILED"}

def install(app): app.include_router(router)
