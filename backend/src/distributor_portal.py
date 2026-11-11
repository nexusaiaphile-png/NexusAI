"""NexusAI Global Distributor Portal.

Separate distributor identities and performance-referral ledger. Does not alter
installer partner accounts, deployments, or their existing R100 proposal.
"""
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

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/distributors", tags=["global-distributors"])
admin_router = APIRouter(prefix="/api/admin/distributors", tags=["global-distributors-admin"])
DB_PATH = Path(__file__).resolve().parents[2] / "nexusai.db"
SESSION_TTL = 12 * 60 * 60
REGISTER_RATE: dict[str, list[float]] = {}
LOGIN_RATE: dict[str, list[float]] = {}


def _postgres() -> bool:
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


def _sql(conn, query: str, params=()):
    if _postgres():
        cur = conn.cursor()
        cur.execute(query, params)
        return cur
    return conn.execute(query.replace("%s", "?"), params)


def init_distributor_db():
    with _db() as conn:
        statements = [
            """CREATE TABLE IF NOT EXISTS nexusai_distributor_accounts (
                distributor_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL, company_name TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT 'South Africa', password_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', created_at DOUBLE PRECISION NOT NULL,
                activated_at DOUBLE PRECISION)""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_distributor_email_lower ON nexusai_distributor_accounts(LOWER(email))",
            """CREATE TABLE IF NOT EXISTS nexusai_distributor_sessions (
                token_hash TEXT PRIMARY KEY, distributor_id TEXT NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                revoked_at DOUBLE PRECISION)""",
            """CREATE TABLE IF NOT EXISTS nexusai_distributor_referrals (
                referral_id TEXT PRIMARY KEY, distributor_id TEXT NOT NULL,
                installer_company TEXT NOT NULL, contact_name TEXT NOT NULL,
                contact_email TEXT NOT NULL, contact_phone TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL, province TEXT NOT NULL DEFAULT '',
                relationship TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                product_brand TEXT NOT NULL DEFAULT 'NEXUSAI',
                status TEXT NOT NULL DEFAULT 'SUBMITTED', verified_cameras INTEGER NOT NULL DEFAULT 0,
                reward_amount_rands NUMERIC, created_at DOUBLE PRECISION NOT NULL,
                reviewed_at DOUBLE PRECISION, reviewed_by TEXT)""",
            """CREATE TABLE IF NOT EXISTS nexusai_distributor_rewards (
                reward_id TEXT PRIMARY KEY, referral_id TEXT NOT NULL UNIQUE,
                distributor_id TEXT NOT NULL, amount_rands NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', created_at DOUBLE PRECISION NOT NULL,
                paid_at DOUBLE PRECISION, payment_reference TEXT)""",
            """CREATE TABLE IF NOT EXISTS nexusai_distributor_agreements (
                agreement_id TEXT PRIMARY KEY, distributor_id TEXT NOT NULL,
                agreement_version TEXT NOT NULL, signed_reference TEXT NOT NULL,
                recorded_at DOUBLE PRECISION NOT NULL, recorded_by TEXT NOT NULL,
                UNIQUE(distributor_id, agreement_version))""",
        ]
        for statement in statements:
            _sql(conn, statement)
        if _postgres():
            _sql(conn, "ALTER TABLE nexusai_distributor_referrals ADD COLUMN IF NOT EXISTS product_brand TEXT NOT NULL DEFAULT 'NEXUSAI'")
        else:
            columns = {row[1] for row in _sql(conn, "PRAGMA table_info(nexusai_distributor_referrals)").fetchall()}
            if "product_brand" not in columns:
                _sql(conn, "ALTER TABLE nexusai_distributor_referrals ADD COLUMN product_brand TEXT NOT NULL DEFAULT 'NEXUSAI'")
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_distributor_referrals_owner ON nexusai_distributor_referrals(distributor_id, created_at)")
        _sql(conn, "CREATE INDEX IF NOT EXISTS idx_distributor_rewards_owner ON nexusai_distributor_rewards(distributor_id, created_at)")
        _sql(conn, """CREATE UNIQUE INDEX IF NOT EXISTS idx_distributor_referral_contact_active ON nexusai_distributor_referrals(LOWER(contact_email)) WHERE status <> 'REJECTED'""")


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


ADMIN_EMAIL = "nexusai.aphile@gmail.com"


def _verify_google_identity_token(credential: str, client_id: str) -> dict:
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(credential, GoogleRequest(), audience=client_id)


def _admin(authorization: str | None):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not client_id:
        raise HTTPException(503, "Google administrator sign-in is not configured.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in with the authorised NexusAI Google account.")
    credential = authorization.split(" ", 1)[1].strip()
    if not credential or len(credential) > 5000:
        raise HTTPException(401, "Administrator sign-in is invalid. Please sign in again.")
    try:
        claims = _verify_google_identity_token(credential, client_id)
    except Exception:
        raise HTTPException(401, "Google sign-in could not be verified. Please sign in again.")
    email = str(claims.get("email", "")).strip().lower()
    if email != ADMIN_EMAIL or claims.get("email_verified") is not True:
        raise HTTPException(403, "This Google account is not authorised to administer NexusAI.")


@admin_router.get("/auth-config")
async def admin_auth_config():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    return {"configured": bool(client_id), "client_id": client_id if client_id else None,
            "administrator_email": ADMIN_EMAIL}


def _rate_limit(bucket: dict, host: str, limit: int, window: int):
    now = time.time()
    attempts = [stamp for stamp in bucket.get(host, []) if now - stamp < window]
    if len(attempts) >= limit:
        raise HTTPException(429, "Too many attempts. Please wait and try again.")
    attempts.append(now)
    bucket[host] = attempts


def _issue_session(conn, distributor_id: str) -> str:
    token = secrets.token_urlsafe(40)
    now = time.time()
    _sql(conn, "INSERT INTO nexusai_distributor_sessions(token_hash,distributor_id,expires_at,created_at) VALUES(%s,%s,%s,%s)",
         (_hash(token), distributor_id, now + SESSION_TTL, now))
    return token


def _current_distributor(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in to your distributor account.")
    token = authorization.split(" ", 1)[1].strip()
    if not token or len(token) > 200:
        raise HTTPException(401, "Please sign in to your distributor account.")
    with _db() as conn:
        row = _sql(conn, """SELECT a.distributor_id,a.email,a.full_name,a.company_name,a.country,a.status
            FROM nexusai_distributor_sessions s JOIN nexusai_distributor_accounts a
            ON a.distributor_id=s.distributor_id
            WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>%s AND a.status='ACTIVE'""",
            (_hash(token), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired or your account is not active. Please sign in again.")
    keys = ["distributor_id", "email", "full_name", "company_name", "country", "status"]
    return dict(zip(keys, row)), token


class RegisterDistributor(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=160)
    company_name: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=5, max_length=200)
    country: str = Field(default="South Africa", min_length=2, max_length=100)
    password: str = Field(..., min_length=12, max_length=128)


class DistributorLogin(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=1, max_length=128)


class InstallerReferral(BaseModel):
    installer_company: str = Field(..., min_length=2, max_length=200)
    contact_name: str = Field(..., min_length=2, max_length=160)
    contact_email: str = Field(..., min_length=5, max_length=200)
    contact_phone: str = Field(default="", max_length=40)
    country: str = Field(..., min_length=2, max_length=100)
    province: str = Field(default="", max_length=100)
    relationship: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=1000)
    confirms_permission: bool = Field(...)


class DistributorStatus(BaseModel):
    status: str = Field(..., min_length=6, max_length=10)
    agreement_version: str | None = Field(default=None, min_length=2, max_length=40)
    agreement_reference: str | None = Field(default=None, min_length=2, max_length=200)


class ReferralReview(BaseModel):
    status: str = Field(..., min_length=7, max_length=10)
    verified_cameras: int = Field(default=0, ge=0, le=1000000)


class RewardPayment(BaseModel):
    payment_reference: str = Field(..., min_length=2, max_length=120)


@router.post("/register")
async def register_distributor(payload: RegisterDistributor, request: Request):
    email = payload.email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "Enter a valid email address.")
    full_name, company_name = payload.full_name.strip(), payload.company_name.strip()
    if not full_name or not company_name:
        raise HTTPException(400, "Enter your full name and company name.")
    host = request.client.host if request.client else "unknown"
    _rate_limit(REGISTER_RATE, host, 5, 1800)
    now = time.time()
    distributor_id = "dist_" + uuid.uuid4().hex[:16]
    with _db() as conn:
        exists = _sql(conn, "SELECT 1 FROM nexusai_distributor_accounts WHERE LOWER(email)=%s", (email,)).fetchone()
        if exists:
            raise HTTPException(409, "An account already exists for this email.")
        _sql(conn, """INSERT INTO nexusai_distributor_accounts
            (distributor_id,email,full_name,company_name,country,password_hash,status,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,'PENDING',%s)""",
            (distributor_id, email, full_name, company_name, payload.country.strip(),
             _password_hash(payload.password), now))
    return {"registered": True, "status": "PENDING",
            "message": "Application received. NexusAI must approve your distributor account before sign-in."}


@router.post("/login")
async def login_distributor(payload: DistributorLogin, request: Request):
    host = request.client.host if request.client else "unknown"
    _rate_limit(LOGIN_RATE, host, 10, 600)
    email = payload.email.strip().lower()
    with _db() as conn:
        row = _sql(conn, "SELECT distributor_id,password_hash,status FROM nexusai_distributor_accounts WHERE LOWER(email)=%s",
                   (email,)).fetchone()
        if not row or not _check_password(payload.password, row[1]):
            raise HTTPException(401, "Email or password is incorrect.")
        if row[2] == "PENDING":
            raise HTTPException(403, "Your distributor application is awaiting NexusAI approval.")
        if row[2] != "ACTIVE":
            raise HTTPException(403, "This distributor account is not active. Contact NexusAI.")
        token = _issue_session(conn, row[0])
    return {"authenticated": True, "access_token": token, "token_type": "bearer", "expires_in": SESSION_TTL}


@router.get("/me")
async def distributor_dashboard(authorization: str | None = Header(default=None)):
    distributor, _ = _current_distributor(authorization)
    with _db() as conn:
        referrals = _sql(conn, """SELECT referral_id,installer_company,contact_name,contact_email,country,
            province,status,verified_cameras,reward_amount_rands,created_at,reviewed_at
            FROM nexusai_distributor_referrals WHERE distributor_id=%s AND product_brand='NEXUSAI' ORDER BY created_at DESC LIMIT 500""",
            (distributor["distributor_id"],)).fetchall()
        rewards = _sql(conn, """SELECT reward_id,referral_id,amount_rands,status,created_at,paid_at,payment_reference
            FROM nexusai_distributor_rewards WHERE distributor_id=%s ORDER BY created_at DESC LIMIT 500""",
            (distributor["distributor_id"],)).fetchall()
    referral_keys = ["referral_id","installer_company","contact_name","contact_email","country","province",
                     "status","verified_cameras","reward_amount_rands","created_at","reviewed_at"]
    reward_keys = ["reward_id","referral_id","amount_rands","status","created_at","paid_at","payment_reference"]
    referral_data = [dict(zip(referral_keys, row)) for row in referrals]
    reward_data = [dict(zip(reward_keys, row)) for row in rewards]
    return {"distributor": distributor, "summary": {
        "referrals_submitted": len(referral_data),
        "qualified_referrals": sum(1 for item in referral_data if item["status"] == "CONVERTED"),
        "verified_cameras": sum(int(item["verified_cameras"] or 0) for item in referral_data),
        "rewards_pending_rands": sum(float(item["amount_rands"]) for item in reward_data if item["status"] == "PENDING"),
        "rewards_paid_rands": sum(float(item["amount_rands"]) for item in reward_data if item["status"] == "PAID"),
    }, "referrals": referral_data, "rewards": reward_data}


@router.post("/referrals")
async def submit_referral(payload: InstallerReferral, authorization: str | None = Header(default=None)):
    distributor, _ = _current_distributor(authorization)
    if not payload.confirms_permission:
        raise HTTPException(400, "Confirm that the installer has agreed to be introduced to NexusAI.")
    email = payload.contact_email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "Enter a valid installer contact email.")
    referral_id = "dref_" + uuid.uuid4().hex[:16]
    with _db() as conn:
        duplicate = _sql(conn, """SELECT 1 FROM nexusai_distributor_referrals
            WHERE LOWER(contact_email)=%s AND status NOT IN ('REJECTED')""", (email,)).fetchone()
        if duplicate:
            raise HTTPException(409, "This installer contact has already been referred. Contact NexusAI if attribution needs review.")
        _sql(conn, """INSERT INTO nexusai_distributor_referrals
            (referral_id,distributor_id,installer_company,contact_name,contact_email,contact_phone,country,
             province,relationship,notes,product_brand,status,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'NEXUSAI','SUBMITTED',%s)""",
            (referral_id, distributor["distributor_id"], payload.installer_company.strip(),
             payload.contact_name.strip(), email, payload.contact_phone.strip(), payload.country.strip(),
             payload.province.strip(), payload.relationship.strip(), payload.notes.strip(), time.time()))
    return {"submitted": True, "referral_id": referral_id, "status": "SUBMITTED",
            "message": "Referral submitted for review. No reward is earned for contact details or an introduction alone."}


@router.post("/logout")
async def logout_distributor(authorization: str | None = Header(default=None)):
    distributor, token = _current_distributor(authorization)
    with _db() as conn:
        _sql(conn, "UPDATE nexusai_distributor_sessions SET revoked_at=%s WHERE token_hash=%s AND distributor_id=%s",
             (time.time(), _hash(token), distributor["distributor_id"]))
    return {"logged_out": True}


@admin_router.get("")
async def admin_list_distributors(authorization: str | None = Header(default=None)):
    _admin(authorization)
    with _db() as conn:
        rows = _sql(conn, """SELECT a.distributor_id,a.email,a.full_name,a.company_name,a.country,a.status,a.created_at,
            COUNT(r.referral_id) AS referrals,
            COALESCE(SUM(CASE WHEN r.status='CONVERTED' THEN r.verified_cameras ELSE 0 END),0) AS verified_cameras
            FROM nexusai_distributor_accounts a LEFT JOIN nexusai_distributor_referrals r
            ON r.distributor_id=a.distributor_id AND r.product_brand='NEXUSAI' GROUP BY a.distributor_id ORDER BY a.created_at DESC""").fetchall()
    keys = ["distributor_id","email","full_name","company_name","country","status","created_at","referrals","verified_cameras"]
    return {"distributors": [dict(zip(keys, row)) for row in rows]}


@admin_router.post("/{distributor_id}/status")
async def admin_distributor_status(distributor_id: str, payload: DistributorStatus,
                                   authorization: str | None = Header(default=None)):
    _admin(authorization)
    status = payload.status.strip().upper()
    if status not in {"ACTIVE", "SUSPENDED"}:
        raise HTTPException(400, "Status must be ACTIVE or SUSPENDED.")
    if status == "ACTIVE" and (
        not payload.agreement_version or not payload.agreement_version.strip()
        or not payload.agreement_reference or not payload.agreement_reference.strip()
    ):
        raise HTTPException(400, "Record the signed distributor agreement version and reference before activation.")
    with _db() as conn:
        cur = _sql(conn, """UPDATE nexusai_distributor_accounts SET status=%s
            WHERE distributor_id=%s AND status IN ('PENDING','ACTIVE','SUSPENDED')""", (status, distributor_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Distributor account not found.")
        if status == "ACTIVE":
            _sql(conn, """INSERT INTO nexusai_distributor_agreements
                (agreement_id,distributor_id,agreement_version,signed_reference,recorded_at,recorded_by)
                VALUES(%s,%s,%s,%s,%s,'NEXUSAI_ADMIN')
                ON CONFLICT(distributor_id,agreement_version) DO UPDATE SET
                signed_reference=excluded.signed_reference,recorded_at=excluded.recorded_at,recorded_by=excluded.recorded_by""",
                ("dagr_" + uuid.uuid4().hex[:16], distributor_id, payload.agreement_version.strip(),
                 payload.agreement_reference.strip(), time.time()))
        if status == "SUSPENDED":
            _sql(conn, "UPDATE nexusai_distributor_sessions SET revoked_at=%s WHERE distributor_id=%s AND revoked_at IS NULL",
                 (time.time(), distributor_id))
    return {"updated": True, "distributor_id": distributor_id, "status": status}


@admin_router.get("/referrals")
async def admin_list_distributor_referrals(status: str = "ALL",
                                           authorization: str | None = Header(default=None)):
    _admin(authorization)
    normalized = status.strip().upper()
    if normalized not in {"ALL", "SUBMITTED", "CONTACTED", "QUALIFIED", "CONVERTED", "REJECTED"}:
        raise HTTPException(400, "Invalid referral status.")
    with _db() as conn:
        if normalized == "ALL":
            rows = _sql(conn, """SELECT r.referral_id,r.distributor_id,a.company_name AS distributor_company,
                a.email AS distributor_email,r.installer_company,r.contact_name,r.contact_email,r.contact_phone,
                r.country,r.province,r.relationship,r.notes,r.status,r.verified_cameras,r.reward_amount_rands,
                r.created_at,r.reviewed_at FROM nexusai_distributor_referrals r
                JOIN nexusai_distributor_accounts a ON a.distributor_id=r.distributor_id
                WHERE r.product_brand='NEXUSAI' ORDER BY r.created_at DESC LIMIT 1000""").fetchall()
        else:
            rows = _sql(conn, """SELECT r.referral_id,r.distributor_id,a.company_name AS distributor_company,
                a.email AS distributor_email,r.installer_company,r.contact_name,r.contact_email,r.contact_phone,
                r.country,r.province,r.relationship,r.notes,r.status,r.verified_cameras,r.reward_amount_rands,
                r.created_at,r.reviewed_at FROM nexusai_distributor_referrals r
                JOIN nexusai_distributor_accounts a ON a.distributor_id=r.distributor_id
                WHERE r.product_brand='NEXUSAI' AND r.status=%s ORDER BY r.created_at DESC LIMIT 1000""", (normalized,)).fetchall()
    keys = ["referral_id","distributor_id","distributor_company","distributor_email","installer_company","contact_name",
            "contact_email","contact_phone","country","province","relationship","notes","status","verified_cameras",
            "reward_amount_rands","created_at","reviewed_at"]
    return {"referrals": [dict(zip(keys, row)) for row in rows]}


@admin_router.post("/referrals/{referral_id}/review")
async def admin_review_referral(referral_id: str, payload: ReferralReview,
                                authorization: str | None = Header(default=None)):
    _admin(authorization)
    status = payload.status.strip().upper()
    if status not in {"CONTACTED", "QUALIFIED", "CONVERTED", "REJECTED"}:
        raise HTTPException(400, "Status must be CONTACTED, QUALIFIED, CONVERTED or REJECTED.")
    if status == "CONVERTED" and payload.verified_cameras < 1:
        raise HTTPException(400, "A converted referral requires at least one verified camera.")
    now = time.time()
    with _db() as conn:
        row = _sql(conn, "SELECT distributor_id,status FROM nexusai_distributor_referrals WHERE referral_id=%s", (referral_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Referral not found.")
        if row[1] in {"CONVERTED", "REJECTED"}:
            raise HTTPException(409, "This referral is closed and cannot be reviewed again.")
        if status == "CONVERTED" and row[1] != "QUALIFIED":
            raise HTTPException(409, "A referral must be marked QUALIFIED before it can be converted.")
        updated = _sql(conn, """UPDATE nexusai_distributor_referrals SET status=%s,verified_cameras=%s,
            reviewed_at=%s,reviewed_by='NEXUSAI_ADMIN'
            WHERE referral_id=%s AND status NOT IN ('CONVERTED','REJECTED')""",
            (status, payload.verified_cameras if status == "CONVERTED" else 0, now, referral_id))
        if updated.rowcount != 1:
            raise HTTPException(409, "This referral has already been converted or reviewed concurrently.")
    return {"reviewed": True, "referral_id": referral_id, "status": status,
            "reward_created": False,
            "message": "Camera count recorded for tracking only. Monthly commission is payable only after the related subscription payment is verified."}


@admin_router.get("/rewards")
async def admin_list_distributor_rewards(status: str = "ALL",
                                         authorization: str | None = Header(default=None)):
    _admin(authorization)
    normalized = status.strip().upper()
    if normalized not in {"ALL", "PENDING", "PAID"}:
        raise HTTPException(400, "Status must be ALL, PENDING or PAID.")
    with _db() as conn:
        if normalized == "ALL":
            rows = _sql(conn, """SELECT w.reward_id,w.referral_id,w.distributor_id,a.company_name,a.email,
                w.amount_rands,w.status,w.created_at,w.paid_at,w.payment_reference
                FROM nexusai_distributor_rewards w JOIN nexusai_distributor_accounts a
                ON a.distributor_id=w.distributor_id ORDER BY w.created_at DESC LIMIT 1000""").fetchall()
        else:
            rows = _sql(conn, """SELECT w.reward_id,w.referral_id,w.distributor_id,a.company_name,a.email,
                w.amount_rands,w.status,w.created_at,w.paid_at,w.payment_reference
                FROM nexusai_distributor_rewards w JOIN nexusai_distributor_accounts a
                ON a.distributor_id=w.distributor_id WHERE w.status=%s
                ORDER BY w.created_at DESC LIMIT 1000""", (normalized,)).fetchall()
    keys = ["reward_id","referral_id","distributor_id","company_name","email","amount_rands","status","created_at","paid_at","payment_reference"]
    return {"rewards": [dict(zip(keys, row)) for row in rows]}


@admin_router.post("/rewards/{reward_id}/pay")
async def admin_mark_reward_paid(reward_id: str, payload: RewardPayment,
                                 authorization: str | None = Header(default=None)):
    _admin(authorization)
    payment_reference = payload.payment_reference.strip()
    if len(payment_reference) < 2:
        raise HTTPException(400, "Enter a valid payment reference.")
    with _db() as conn:
        agreement = _sql(conn, """SELECT 1 FROM nexusai_distributor_agreements a
            JOIN nexusai_distributor_rewards r ON r.distributor_id=a.distributor_id
            WHERE r.reward_id=%s LIMIT 1""", (reward_id,)).fetchone()
        if not agreement:
            raise HTTPException(409, "A recorded signed distributor agreement is required before payment.")
        cur = _sql(conn, """UPDATE nexusai_distributor_rewards SET status='PAID',paid_at=%s,payment_reference=%s
            WHERE reward_id=%s AND status='PENDING'""", (time.time(), payment_reference, reward_id))
        if cur.rowcount != 1:
            raise HTTPException(404, "Pending reward not found.")
    return {"paid": True, "reward_id": reward_id}
