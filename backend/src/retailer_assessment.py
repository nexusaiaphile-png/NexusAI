"""Retailer compatibility assessment intake with isolated PDF storage and admin review."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, Response as FastAPIResponse, UploadFile
from fastapi.responses import Response

router = APIRouter(tags=["retailer-assessment"])
admin_router = APIRouter(tags=["retailer-assessment-admin"])
DB_PATH = Path(__file__).resolve().parents[2] / "nexusai.db"
RATE = {}
LOGIN_RATE = {}
SESSION_COOKIE = "nexusai_retail_admin_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_PDF_BYTES = 10 * 1024 * 1024
STATUSES = {"NEW", "UNDER_REVIEW", "NEED_INFO", "COMPATIBLE", "NOT_COMPATIBLE", "PILOT_APPROVED", "CLOSED"}

def _pg():
    return bool(os.getenv("DATABASE_URL", "").strip())

@contextmanager
def _db():
    if _pg():
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            yield conn
    else:
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

def _sql(conn, query, params=()):
    if _pg():
        cur = conn.cursor()
        cur.execute(query, params)
        return cur
    return conn.execute(query.replace("%s", "?"), params)

def init_db():
    blob = "BYTEA" if _pg() else "BLOB"
    with _db() as conn:
        _sql(conn, f"""CREATE TABLE IF NOT EXISTS nexusai_retail_assessments (
            application_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, website TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL, contact_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', store_count TEXT NOT NULL DEFAULT '',
            camera_count TEXT NOT NULL DEFAULT '', cctv_provider TEXT NOT NULL DEFAULT '',
            uses_hikvision TEXT NOT NULL DEFAULT '', pos_provider TEXT NOT NULL,
            pos_version TEXT NOT NULL DEFAULT '', pos_api TEXT NOT NULL DEFAULT '',
            objectives TEXT NOT NULL, additional_notes TEXT NOT NULL DEFAULT '',
            pdf_filename TEXT NOT NULL, pdf_mime TEXT NOT NULL, pdf_sha256 TEXT NOT NULL,
            pdf_data {blob} NOT NULL, status TEXT NOT NULL DEFAULT 'NEW',
            admin_notes TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL)""")
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_retail_assessments_created ON nexusai_retail_assessments(created_at)")
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_retail_assessments_status ON nexusai_retail_assessments(status)")

def _session_secret():
    secret = os.getenv("RETAIL_ASSESSMENT_SESSION_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Administrator login is not configured.")
    return secret.encode("utf-8")

def _admin(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        issued_text, signature = token.split(".", 1)
        issued = int(issued_text)
        if issued > int(time.time()) + 60 or int(time.time()) - issued > SESSION_SECONDS:
            raise ValueError("expired")
        expected = hmac.new(_session_secret(), issued_text.encode("ascii"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature, expected):
            raise ValueError("invalid")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Please sign in to the retailer assessment admin.")

def _clean(value, limit=500):
    value = re.sub(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]", "", (value or "")).strip()
    return value[:limit]

@router.post("/api/retailer-assessments")
async def submit_assessment(
    request: Request,
    company_name: str = Form(..., min_length=2, max_length=200),
    website: str = Form(default="", max_length=250),
    country: str = Form(..., min_length=2, max_length=100),
    contact_name: str = Form(..., min_length=2, max_length=160),
    role: str = Form(default="", max_length=120),
    email: str = Form(..., min_length=5, max_length=200),
    phone: str = Form(default="", max_length=50),
    store_count: str = Form(default="", max_length=80),
    camera_count: str = Form(default="", max_length=80),
    cctv_provider: str = Form(default="", max_length=120),
    uses_hikvision: str = Form(default="", max_length=40),
    pos_provider: str = Form(..., min_length=2, max_length=160),
    pos_version: str = Form(default="", max_length=120),
    pos_api: str = Form(default="", max_length=120),
    objectives: str = Form(..., min_length=5, max_length=2000),
    additional_notes: str = Form(default="", max_length=2000),
    privacy_acknowledged: str = Form(...),
    technical_pdf: UploadFile = File(...)
):
    host = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in RATE.get(host, []) if now - t < 3600]
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many applications from this connection. Please try again later.")
    if privacy_acknowledged.lower() not in {"true", "1", "yes", "on"}:
        raise HTTPException(400, "Please acknowledge the privacy notice before submitting.")
    email = _clean(email, 200).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "Enter a valid business email address.")
    filename = Path(technical_pdf.filename or "technical-document.pdf").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF technical documents are accepted.")
    if technical_pdf.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(400, "The uploaded document must be a PDF.")
    data = await technical_pdf.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "The PDF exceeds the 10 MB upload limit.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "The uploaded file does not appear to be a valid PDF.")
    RATE[host] = attempts + [now]
    app_id = "RA-" + uuid.uuid4().hex[:12].upper()
    digest = hashlib.sha256(data).hexdigest()
    values = (
        app_id, _clean(company_name,200), _clean(website,250), _clean(country,100),
        _clean(contact_name,160), _clean(role,120), email, _clean(phone,50),
        _clean(store_count,80), _clean(camera_count,80), _clean(cctv_provider,120),
        _clean(uses_hikvision,40), _clean(pos_provider,160), _clean(pos_version,120),
        _clean(pos_api,120), _clean(objectives,2000), _clean(additional_notes,2000),
        filename, "application/pdf", digest, data, "NEW", "", now, now
    )
    with _db() as conn:
        _sql(conn, """INSERT INTO nexusai_retail_assessments
        (application_id,company_name,website,country,contact_name,role,email,phone,store_count,camera_count,
        cctv_provider,uses_hikvision,pos_provider,pos_version,pos_api,objectives,additional_notes,
        pdf_filename,pdf_mime,pdf_sha256,pdf_data,status,admin_notes,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", values)
    return {"submitted": True, "application_id": app_id,
            "message": "Your information has been submitted for a NexusAI compatibility assessment. We will review the technical details and contact you."}

@admin_router.post("/api/admin/retailer-assessments/login")
async def assessment_admin_login(request: Request, response: FastAPIResponse):
    host = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in LOGIN_RATE.get(host, []) if now - t < 900]
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many password attempts. Please try again in 15 minutes.")
    body = await request.json()
    supplied = str(body.get("password", ""))
    expected = os.getenv("RETAIL_ASSESSMENT_ADMIN_PASSWORD", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        LOGIN_RATE[host] = attempts + [now]
        raise HTTPException(401, "Incorrect password.")
    LOGIN_RATE.pop(host, None)
    issued = str(int(now))
    signature = hmac.new(_session_secret(), issued.encode("ascii"), hashlib.sha256).hexdigest()
    response.set_cookie(SESSION_COOKIE, issued + "." + signature, max_age=SESSION_SECONDS,
                        httponly=True, secure=True, samesite="strict",
                        path="/api/admin/retailer-assessments")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}

@admin_router.post("/api/admin/retailer-assessments/logout")
async def assessment_admin_logout(response: FastAPIResponse):
    response.delete_cookie(SESSION_COOKIE, path="/api/admin/retailer-assessments",
                           secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"logged_out": True}

@admin_router.get("/api/admin/retailer-assessments")
async def list_assessments(request: Request):
    _admin(request)
    with _db() as conn:
        rows = _sql(conn, """SELECT application_id,company_name,website,country,contact_name,role,email,phone,
        store_count,camera_count,cctv_provider,uses_hikvision,pos_provider,pos_version,pos_api,objectives,
        additional_notes,pdf_filename,pdf_sha256,status,admin_notes,created_at,updated_at
        FROM nexusai_retail_assessments ORDER BY created_at DESC LIMIT 300""").fetchall()
    keys = ["application_id","company_name","website","country","contact_name","role","email","phone",
        "store_count","camera_count","cctv_provider","uses_hikvision","pos_provider","pos_version","pos_api",
        "objectives","additional_notes","pdf_filename","pdf_sha256","status","admin_notes","created_at","updated_at"]
    return {"applications":[dict(zip(keys,row)) for row in rows]}

@admin_router.get("/api/admin/retailer-assessments/{application_id}/pdf")
async def download_assessment_pdf(application_id: str, request: Request):
    _admin(request)
    with _db() as conn:
        row = _sql(conn, "SELECT pdf_data,pdf_filename,pdf_sha256 FROM nexusai_retail_assessments WHERE application_id=%s",
                   (application_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Application not found.")
    data, filename, digest = row[0], row[1], row[2]
    if hashlib.sha256(bytes(data)).hexdigest() != digest:
        raise HTTPException(500, "Stored document integrity check failed.")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "technical-document.pdf"
    return Response(content=bytes(data), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"', "Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"})

@admin_router.patch("/api/admin/retailer-assessments/{application_id}")
async def update_assessment(application_id: str, request: Request):
    _admin(request)
    body = await request.json()
    status = str(body.get("status","")).strip().upper()
    notes = _clean(str(body.get("admin_notes","")), 3000)
    if status not in STATUSES:
        raise HTTPException(400, "Choose a valid assessment status.")
    with _db() as conn:
        result = _sql(conn, """UPDATE nexusai_retail_assessments SET status=%s,admin_notes=%s,updated_at=%s
            WHERE application_id=%s""", (status, notes, time.time(), application_id))
        if result.rowcount != 1:
            raise HTTPException(404, "Application not found.")
    return {"updated": True, "application_id": application_id, "status": status}

def install(app):
    # Register endpoints before touching the database. Authentication itself is
    # independent of DB availability; a DB startup problem must not turn the
    # admin login URL into a misleading 404.
    app.include_router(router)
    app.include_router(admin_router)
    init_db()
