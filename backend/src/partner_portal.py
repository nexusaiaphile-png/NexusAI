"""Private NexusAI partner portal: invitations, isolated accounts and verified deployment tracking."""
import hashlib
import hmac
import os
import secrets
import smtplib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/partners", tags=["partner-portal"])
admin_router = APIRouter(prefix="/api/admin/partners", tags=["partner-portal-admin"])
DB_PATH = Path(__file__).resolve().parents[2] / "nexusai.db"
SESSION_TTL = 12 * 60 * 60
INVITE_TTL = 24 * 60 * 60
RATE = {}


def _postgres():
    return bool(os.getenv("DATABASE_URL", "").strip())


@contextmanager
def _db():
    if _postgres():
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
    if _postgres():
        cur = conn.cursor()
        cur.execute(query, params)
        return cur
    return conn.execute(query.replace("%s", "?"), params)


def init_partner_db():
    with _db() as conn:
        statements = [
            """CREATE TABLE IF NOT EXISTS nexusai_partner_accounts (
                partner_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
                company_name TEXT NOT NULL, password_hash TEXT, status TEXT NOT NULL DEFAULT 'INVITED',
                created_at DOUBLE PRECISION NOT NULL, activated_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS nexusai_partner_invites (
                token_hash TEXT PRIMARY KEY, partner_id TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, used_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS nexusai_partner_sessions (
                token_hash TEXT PRIMARY KEY, partner_id TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL, revoked_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS nexusai_partner_deployments (
                deployment_id TEXT PRIMARY KEY, partner_id TEXT NOT NULL, site_reference TEXT NOT NULL,
                business_name TEXT NOT NULL, camera_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                commission_rate INTEGER NOT NULL DEFAULT 100, notes TEXT, created_at DOUBLE PRECISION NOT NULL,
                reviewed_at DOUBLE PRECISION, reviewed_by TEXT)""",
        ]
        for statement in statements:
            _sql(conn, statement)
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_partner_deployments_partner ON nexusai_partner_deployments(partner_id, created_at)")
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_partner_sessions_partner ON nexusai_partner_sessions(partner_id, expires_at)")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return "pbkdf2_sha256$310000$" + salt.hex() + "$" + digest.hex()


def _check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _admin(key: str | None):
    expected = os.getenv("NEXUSAI_ADMIN_KEY", "").strip()
    if not expected or not key or not hmac.compare_digest(key.strip(), expected):
        raise HTTPException(401, "NexusAI administrator access is required.")


def _send_invite(email: str, name: str, token: str):
    host = os.getenv("PARTNER_SMTP_HOST", "").strip()
    user = os.getenv("PARTNER_SMTP_USER", "").strip()
    password = os.getenv("PARTNER_SMTP_PASSWORD", "")
    sender = os.getenv("PARTNER_SMTP_FROM", user).strip()
    url = os.getenv("PARTNER_PORTAL_URL", "https://nexusai-partner-network-preview.onrender.com/partner-login.html").rstrip("/")
    if not all((host, user, password, sender)):
        raise HTTPException(503, "Partner invitation email is not configured yet. Set PARTNER_SMTP_HOST, PARTNER_SMTP_USER, PARTNER_SMTP_PASSWORD and PARTNER_SMTP_FROM.")
    message = EmailMessage()
    message["Subject"] = "Your NexusAI Partner Portal invitation"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Hello {name},\n\nNexusAI has invited you to create your private partner account. "
        f"This one-time invitation expires in 24 hours.\n\n"
        f"Set your password here: {url}?invite={quote(token)}\n\n"
        "Never share your password or invitation link. NexusAI will never ask you to send your password by email.\n"
    )
    try:
        with smtplib.SMTP(host, int(os.getenv("PARTNER_SMTP_PORT", "587")), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
    except Exception:
        raise HTTPException(502, "The invitation email could not be sent. Check the mail settings and try again.")


def _issue_session(conn, partner_id: str):
    token = secrets.token_urlsafe(40)
    now = time.time()
    _sql(conn, "INSERT INTO nexusai_partner_sessions(token_hash,partner_id,expires_at,created_at) VALUES(%s,%s,%s,%s)",
         (_hash(token), partner_id, now + SESSION_TTL, now))
    return token


def _partner_from_token(token: str):
    if not token or len(token) > 200:
        raise HTTPException(401, "Please sign in to your partner account.")
    with _db() as conn:
        row = _sql(conn, """SELECT a.partner_id,a.email,a.full_name,a.company_name,a.status
            FROM nexusai_partner_sessions s JOIN nexusai_partner_accounts a ON a.partner_id=s.partner_id
            WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>%s AND a.status='ACTIVE'""",
            (_hash(token), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired or your account is disabled. Please sign in again.")
    return dict(zip(["partner_id","email","full_name","company_name","status"], row))


def _bearer(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in to your partner account.")
    return _partner_from_token(authorization.split(" ", 1)[1].strip())


class AcceptInvite(BaseModel):
    token: str = Field(..., min_length=20, max_length=200)
    password: str = Field(..., min_length=12, max_length=128)


class PartnerLogin(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=1, max_length=128)


class InvitePartner(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    full_name: str = Field(..., min_length=2, max_length=160)
    company_name: str = Field(..., min_length=2, max_length=200)


class AddDeployment(BaseModel):
    site_reference: str = Field(..., min_length=2, max_length=120)
    business_name: str = Field(..., min_length=2, max_length=200)
    camera_count: int = Field(..., ge=1, le=10000)
    notes: str = Field(default="", max_length=1000)


@router.post("/accept-invitation")
async def accept_invitation(payload: AcceptInvite):
    now = time.time()
    with _db() as conn:
        invite = _sql(conn, """SELECT partner_id,expires_at,used_at FROM nexusai_partner_invites
            WHERE token_hash=%s""", (_hash(payload.token),)).fetchone()
        if not invite or invite[2] is not None or float(invite[1]) <= now:
            raise HTTPException(400, "This invitation is invalid, expired or already used. Ask NexusAI for a new invitation.")
        partner_id = invite[0]
        _sql(conn, "UPDATE nexusai_partner_accounts SET password_hash=%s,status='ACTIVE',activated_at=%s WHERE partner_id=%s AND status='INVITED'",
             (_password_hash(payload.password), now, partner_id))
        _sql(conn, "UPDATE nexusai_partner_invites SET used_at=%s WHERE token_hash=%s AND used_at IS NULL", (now, _hash(payload.token)))
        token = _issue_session(conn, partner_id)
    return {"authenticated": True, "access_token": token, "token_type": "bearer", "expires_in": SESSION_TTL}


@router.post("/login")
async def partner_login(payload: PartnerLogin, request: Request):
    email = payload.email.strip().lower()
    host = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in RATE.get(host, []) if now - t < 600]
    if len(attempts) >= 10:
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes and try again.")
    attempts.append(now)
    RATE[host] = attempts
    with _db() as conn:
        row = _sql(conn, "SELECT partner_id,password_hash,status FROM nexusai_partner_accounts WHERE lower(email)=%s", (email,)).fetchone()
        if not row or row[2] != "ACTIVE" or not row[1] or not _check_password(payload.password, row[1]):
            raise HTTPException(401, "Email or password is incorrect, or the account is not active.")
        token = _issue_session(conn, row[0])
    return {"authenticated": True, "access_token": token, "token_type": "bearer", "expires_in": SESSION_TTL}


@router.get("/me")
async def partner_dashboard(authorization: str | None = Header(default=None)):
    partner = _bearer(authorization)
    with _db() as conn:
        rows = _sql(conn, """SELECT deployment_id,site_reference,business_name,camera_count,status,
            commission_rate,created_at,reviewed_at FROM nexusai_partner_deployments
            WHERE partner_id=%s ORDER BY created_at DESC LIMIT 500""", (partner["partner_id"],)).fetchall()
    deployments = [dict(zip(["deployment_id","site_reference","business_name","camera_count","status","commission_rate","created_at","reviewed_at"], row)) for row in rows]
    total = sum(int(x["camera_count"]) for x in deployments)
    verified = sum(int(x["camera_count"]) for x in deployments if x["status"] == "VERIFIED")
    pending = sum(int(x["camera_count"]) for x in deployments if x["status"] == "PENDING")
    rejected = sum(int(x["camera_count"]) for x in deployments if x["status"] == "REJECTED")
    earned = sum(int(x["camera_count"]) * int(x["commission_rate"]) for x in deployments if x["status"] == "VERIFIED")
    return {"partner": partner, "summary": {"submitted_cameras": total, "verified_cameras": verified,
        "pending_cameras": pending, "rejected_cameras": rejected, "commission_earned_cents": earned * 100,
        "commission_rate_rands": 100}, "deployments": deployments}


@router.post("/logout")
async def partner_logout(authorization: str | None = Header(default=None)):
    partner = _bearer(authorization)
    raw = authorization.split(" ", 1)[1].strip()
    with _db() as conn:
        _sql(conn, "UPDATE nexusai_partner_sessions SET revoked_at=%s WHERE token_hash=%s AND partner_id=%s",
             (time.time(), _hash(raw), partner["partner_id"]))
    return {"logged_out": True}


@admin_router.post("/invite")
async def admin_invite_partner(payload: InvitePartner, x_nexusai_admin_key: str | None = Header(default=None)):
    _admin(x_nexusai_admin_key)
    email = payload.email.strip().lower()
    if not re_full_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    partner_id = "partner_" + uuid.uuid4().hex[:16]
    token = secrets.token_urlsafe(36)
    now = time.time()
    with _db() as conn:
        exists = _sql(conn, "SELECT 1 FROM nexusai_partner_accounts WHERE lower(email)=%s", (email,)).fetchone()
        if exists:
            raise HTTPException(409, "A partner account already exists for this email.")
        _sql(conn, """INSERT INTO nexusai_partner_accounts(partner_id,email,full_name,company_name,status,created_at)
            VALUES(%s,%s,%s,%s,'INVITED',%s)""", (partner_id,email,payload.full_name.strip(),payload.company_name.strip(),now))
        _sql(conn, "INSERT INTO nexusai_partner_invites(token_hash,partner_id,expires_at,created_at) VALUES(%s,%s,%s,%s)",
             (_hash(token),partner_id,now+INVITE_TTL,now))
    try:
        _send_invite(email, payload.full_name.strip(), token)
    except Exception:
        with _db() as conn:
            _sql(conn, "UPDATE nexusai_partner_accounts SET status='SUSPENDED' WHERE partner_id=%s", (partner_id,))
            _sql(conn, "UPDATE nexusai_partner_invites SET used_at=%s WHERE token_hash=%s", (time.time(),_hash(token)))
        raise
    return {"invited": True, "partner_id": partner_id, "email": email, "expires_in_seconds": INVITE_TTL}


@admin_router.get("")
async def admin_list_partners(x_nexusai_admin_key: str | None = Header(default=None)):
    _admin(x_nexusai_admin_key)
    with _db() as conn:
        rows = _sql(conn, """SELECT a.partner_id,a.email,a.full_name,a.company_name,a.status,a.created_at,
            COALESCE(SUM(CASE WHEN d.status='VERIFIED' THEN d.camera_count ELSE 0 END),0) AS verified_cameras
            FROM nexusai_partner_accounts a LEFT JOIN nexusai_partner_deployments d ON d.partner_id=a.partner_id
            GROUP BY a.partner_id ORDER BY a.created_at DESC""").fetchall()
    return {"partners": [dict(zip(["partner_id","email","full_name","company_name","status","created_at","verified_cameras"],r)) for r in rows]}


@admin_router.post("/{partner_id}/deployments")
async def admin_add_deployment(partner_id: str, payload: AddDeployment, x_nexusai_admin_key: str | None = Header(default=None)):
    _admin(x_nexusai_admin_key)
    deployment_id = "dep_" + uuid.uuid4().hex[:16]
    with _db() as conn:
        exists = _sql(conn, "SELECT 1 FROM nexusai_partner_accounts WHERE partner_id=%s AND status='ACTIVE'", (partner_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Active partner account not found.")
        _sql(conn, """INSERT INTO nexusai_partner_deployments(deployment_id,partner_id,site_reference,business_name,
            camera_count,status,commission_rate,notes,created_at) VALUES(%s,%s,%s,%s,%s,'PENDING',100,%s,%s)""",
             (deployment_id,partner_id,payload.site_reference.strip(),payload.business_name.strip(),payload.camera_count,payload.notes.strip(),time.time()))
    return {"created": True, "deployment_id": deployment_id, "status": "PENDING"}


@admin_router.post("/deployments/{deployment_id}/review")
async def admin_review_deployment(deployment_id: str, status: str, x_nexusai_admin_key: str | None = Header(default=None)):
    _admin(x_nexusai_admin_key)
    normalized = status.strip().upper()
    if normalized not in {"VERIFIED", "REJECTED"}:
        raise HTTPException(400, "Status must be VERIFIED or REJECTED.")
    with _db() as conn:
        cur = _sql(conn, "UPDATE nexusai_partner_deployments SET status=%s,reviewed_at=%s,reviewed_by='NEXUSAI_ADMIN' WHERE deployment_id=%s AND status='PENDING'",
                    (normalized,time.time(),deployment_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Pending deployment record not found.")
    return {"reviewed": True, "deployment_id": deployment_id, "status": normalized}


def re_full_email(value: str) -> bool:
    return bool(__import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))
