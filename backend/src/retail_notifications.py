"""Secure dashboard and browser-push notifications for retailer POS exceptions."""
import hashlib
import json
import logging
import secrets
import time
import uuid
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from . import transaction_correlation as tc
from .distributor_portal import _admin as require_admin

router = APIRouter(tags=["retail-notifications"])
_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="retail-push")
_LOG = logging.getLogger("nexusai.retail_notifications")


def _db():
    return tc._connect()


def _q(conn, sql, params=()):
    return tc._execute(conn, sql, params)


def _admin(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "NexusAI administrator access is required.")
    require_admin(authorization)


def _recipient(token: str | None):
    if not token or len(token) < 32 or len(token) > 256:
        raise HTTPException(401, "A valid retailer access token is required.")
    conn = _db()
    try:
        row = _q(conn, """SELECT r.recipient_id,r.organisation_id,r.role,r.display_name
            FROM nexusai_retail_notification_tokens t
            JOIN nexusai_retail_notification_recipients r ON r.recipient_id=t.recipient_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL
              AND (t.expires_at IS NULL OR t.expires_at>?)
              AND r.active=TRUE""", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "Your retailer dashboard access has expired or been revoked.")
    return dict(zip(("recipient_id","organisation_id","role","display_name"), row))


def _bearer(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please sign in to your retailer dashboard.")
    return _recipient(authorization.split(" ", 1)[1].strip())


class OrganisationIn(BaseModel):
    organisation_id: str = Field(min_length=3, max_length=100)
    organisation_name: str = Field(min_length=2, max_length=200)
    site_ids: list[str] = Field(min_length=1, max_length=500)


class RecipientIn(BaseModel):
    role: str
    display_name: str = Field(min_length=2, max_length=160)
    site_ids: list[str] = Field(min_length=1, max_length=500)


class PushSubscriptionIn(BaseModel):
    subscription: dict


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=20, max_length=2048)


@router.get("/api/admin/retail/sites")
async def list_retail_customer_sites(authorization: str | None = Header(default=None)):
    """List customer branches for the authenticated CEO admin provisioning screen."""
    _admin(authorization)
    conn = _db()
    try:
        rows = _q(conn, """SELECT c.site_id,c.business_name,c.store_address
            FROM nexusai_customers c
            WHERE COALESCE(c.status,'ACTIVE')='ACTIVE'
            ORDER BY c.business_name,c.site_id""").fetchall()
    finally:
        conn.close()
    return {"sites": [{"site_id": r[0], "business_name": r[1], "store_address": r[2]} for r in rows]}


@router.post("/api/admin/retail/organisations")
async def create_organisation(payload: OrganisationIn, authorization: str | None = Header(default=None)):
    _admin(authorization)
    site_ids = list(dict.fromkeys(x.strip() for x in payload.site_ids))
    if len(site_ids) != len(payload.site_ids) or any(not x for x in site_ids):
        raise HTTPException(422, "Branch IDs must be unique and non-empty.")
    conn = _db()
    try:
        placeholders = ",".join(["?"] * len(site_ids))
        found = _q(conn, f"SELECT site_id FROM nexusai_customers WHERE site_id IN ({placeholders})", tuple(site_ids)).fetchall()
        if {r[0] for r in found} != set(site_ids):
            raise HTTPException(422, "One or more branch IDs do not exist in NexusAI customer records.")
        _q(conn, "INSERT INTO nexusai_retail_organisations (organisation_id,organisation_name,active,created_at,updated_at) VALUES (?,?,TRUE,?,?)",
           (payload.organisation_id, payload.organisation_name.strip(), time.time(), time.time()))
        for site_id in site_ids:
            _q(conn, "INSERT INTO nexusai_retail_organisation_sites (organisation_id,site_id,created_at) VALUES (?,?,?)",
               (payload.organisation_id, site_id, time.time()))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, "Organisation already exists or a branch is already assigned.")
        raise
    finally:
        conn.close()
    return {"created": True, "organisation_id": payload.organisation_id, "site_count": len(site_ids)}


@router.post("/api/admin/retail/organisations/{organisation_id}/recipients")
async def create_recipient(organisation_id: str, payload: RecipientIn, authorization: str | None = Header(default=None)):
    _admin(authorization)
    role = payload.role.strip().upper()
    if role not in {"STORE_MANAGER", "HEAD_OFFICE"}:
        raise HTTPException(422, "Role must be STORE_MANAGER or HEAD_OFFICE.")
    site_ids = list(dict.fromkeys(x.strip() for x in payload.site_ids))
    if len(site_ids) != len(payload.site_ids) or any(not x for x in site_ids):
        raise HTTPException(422, "Branch assignments must be unique and non-empty.")
    if role == "STORE_MANAGER" and len(site_ids) != 1:
        raise HTTPException(422, "A store manager must be assigned to exactly one branch.")
    conn = _db()
    recipient_id = "rr-" + uuid.uuid4().hex
    token = secrets.token_urlsafe(40)
    now = time.time()
    try:
        org = _q(conn, "SELECT organisation_id FROM nexusai_retail_organisations WHERE organisation_id=? AND active=TRUE", (organisation_id,)).fetchone()
        if not org:
            raise HTTPException(404, "Active retailer organisation not found.")
        placeholders = ",".join(["?"] * len(site_ids))
        rows = _q(conn, f"SELECT site_id FROM nexusai_retail_organisation_sites WHERE organisation_id=? AND site_id IN ({placeholders})",
                  (organisation_id, *site_ids)).fetchall()
        if {r[0] for r in rows} != set(site_ids):
            raise HTTPException(422, "Every assigned branch must belong to this retailer organisation.")
        _q(conn, """INSERT INTO nexusai_retail_notification_recipients
            (recipient_id,organisation_id,role,display_name,active,dashboard_opt_in,created_at,updated_at)
            VALUES (?,?,?,?,TRUE,TRUE,?,?)""",
           (recipient_id,organisation_id,role,payload.display_name.strip(),now,now))
        for site_id in site_ids:
            _q(conn, "INSERT INTO nexusai_retail_recipient_sites (recipient_id,organisation_id,site_id,created_at) VALUES (?,?,?,?)",
               (recipient_id,organisation_id,site_id,now))
        _q(conn, "INSERT INTO nexusai_retail_notification_tokens (token_hash,recipient_id,created_at,expires_at,revoked_at) VALUES (?,?,?,?,NULL)",
           (hashlib.sha256(token.encode()).hexdigest(),recipient_id,now,now+90*86400))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"created": True, "recipient_id": recipient_id, "role": role, "site_count": len(site_ids),
            "access_token": token, "token_expires_at": now+90*86400,
            "token_notice": "Shown once. Give the access token directly to the authorised recipient."}


class WhiteLabelAssignmentIn(BaseModel):
    organisation_id: str = Field(min_length=3, max_length=100)
    partner_id: str | None = Field(default=None, min_length=3, max_length=100)


class PartnerBrandingIn(BaseModel):
    partner_id: str = Field(min_length=3, max_length=100)
    brand_mode: str = Field(default="CO_BRANDED", max_length=32)
    brand_name: str = Field(min_length=2, max_length=160)
    logo_url: str | None = Field(default=None, max_length=2048)
    primary_color: str = Field(default="#07151d", max_length=7)
    accent_color: str = Field(default="#25f5c5", max_length=7)
    support_email: str | None = Field(default=None, max_length=254)


@router.get("/api/admin/retail/white-label/partners")
async def list_white_label_partners(authorization: str | None = Header(default=None)):
    """List active security partners and their current brand configuration for NexusAI admins."""
    _admin(authorization)
    conn = _db()
    try:
        rows = _q(conn, """SELECT a.partner_id,a.company_name,a.email,a.status,
            b.brand_mode,b.brand_name,b.logo_url,b.primary_color,b.accent_color,b.support_email,b.enabled
            FROM nexusai_partner_accounts a
            LEFT JOIN nexusai_partner_branding b ON b.partner_id=a.partner_id
            WHERE a.status='ACTIVE' ORDER BY a.company_name""").fetchall()
    finally:
        conn.close()
    keys = ("partner_id","company_name","email","status","brand_mode","brand_name",
            "logo_url","primary_color","accent_color","support_email","branding_enabled")
    return {"partners": [dict(zip(keys, row)) for row in rows]}


@router.get("/api/admin/retail/white-label/organisations")
async def list_white_label_organisations(authorization: str | None = Header(default=None)):
    """List retailer organisations and their current security-company assignment."""
    _admin(authorization)
    conn = _db()
    try:
        rows = _q(conn, """SELECT o.organisation_id,o.organisation_name,o.partner_id,a.company_name
            FROM nexusai_retail_organisations o
            LEFT JOIN nexusai_partner_accounts a ON a.partner_id=o.partner_id
            WHERE o.active=TRUE ORDER BY o.organisation_name""").fetchall()
    finally:
        conn.close()
    return {"organisations": [dict(zip(("organisation_id","organisation_name","partner_id","partner_company"), r))
                              for r in rows]}


@router.post("/api/admin/retail/white-label/assign")
async def assign_white_label_partner(payload: WhiteLabelAssignmentIn,
                                     authorization: str | None = Header(default=None)):
    """Assign or unassign a retailer organisation to a verified active security partner."""
    _admin(authorization)
    partner_id = payload.partner_id.strip() if payload.partner_id else None
    conn = _db()
    try:
        org = _q(conn, "SELECT organisation_id FROM nexusai_retail_organisations WHERE organisation_id=? AND active=TRUE",
                 (payload.organisation_id,)).fetchone()
        if not org:
            raise HTTPException(404, "Active retailer organisation not found.")
        if partner_id:
            partner = _q(conn, "SELECT partner_id FROM nexusai_partner_accounts WHERE partner_id=? AND status='ACTIVE'",
                         (partner_id,)).fetchone()
            if not partner:
                raise HTTPException(404, "Active security partner not found. Only approved active partners can be assigned.")
        _q(conn, "UPDATE nexusai_retail_organisations SET partner_id=? WHERE organisation_id=?",
           (partner_id, payload.organisation_id))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"assigned": True, "organisation_id": payload.organisation_id, "partner_id": partner_id}


@router.post("/api/admin/retail/white-label/branding")
async def save_white_label_branding(payload: PartnerBrandingIn,
                                    authorization: str | None = Header(default=None)):
    """Create or update branding for an active security partner; custom domains are deliberately excluded."""
    _admin(authorization)
    mode = payload.brand_mode.strip().upper()
    if mode not in {"FULL_WHITE_LABEL", "CO_BRANDED"}:
        raise HTTPException(422, "Brand mode must be FULL_WHITE_LABEL or CO_BRANDED.")
    brand_name = payload.brand_name.strip()
    if not brand_name:
        raise HTTPException(422, "Enter a customer-facing brand name.")
    primary = payload.primary_color.strip().lower()
    accent = payload.accent_color.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", primary) or not re.fullmatch(r"#[0-9a-f]{6}", accent):
        raise HTTPException(422, "Brand colours must be six-digit hexadecimal values, such as #07151d.")
    logo = (payload.logo_url or "").strip() or None
    if logo:
        parsed = urlsplit(logo)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(422, "Logo URL must be a valid HTTPS URL.")
    support_email = (payload.support_email or "").strip().lower() or None
    if support_email and not re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+", support_email):
        raise HTTPException(422, "Enter a valid support email address.")
    conn = _db()
    now = time.time()
    try:
        partner = _q(conn, "SELECT partner_id FROM nexusai_partner_accounts WHERE partner_id=? AND status='ACTIVE'",
                     (payload.partner_id,)).fetchone()
        if not partner:
            raise HTTPException(404, "Active security partner not found.")
        _q(conn, """INSERT INTO nexusai_partner_branding
            (partner_id,brand_mode,brand_name,logo_url,primary_color,accent_color,support_email,
             custom_domain,enabled,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,NULL,TRUE,?,?)
            ON CONFLICT(partner_id) DO UPDATE SET brand_mode=excluded.brand_mode,
             brand_name=excluded.brand_name,logo_url=excluded.logo_url,
             primary_color=excluded.primary_color,accent_color=excluded.accent_color,
             support_email=excluded.support_email,enabled=TRUE,updated_at=excluded.updated_at""",
           (payload.partner_id,mode,brand_name,logo,primary,accent,support_email,now,now))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"saved": True, "partner_id": payload.partner_id, "brand_mode": mode,
            "brand_name": brand_name, "custom_domain_status": "NOT_CONFIGURED"}


@router.get("/api/retail/notifications/me")
async def retailer_me(authorization: str | None = Header(default=None)):
    recipient = _bearer(authorization)
    conn = _db()
    try:
        rows = _q(conn, """SELECT s.site_id,c.business_name FROM nexusai_retail_recipient_sites s
            JOIN nexusai_customers c ON c.site_id=s.site_id
            WHERE s.recipient_id=? ORDER BY c.business_name""", (recipient["recipient_id"],)).fetchall()
    finally:
        conn.close()
    return {"recipient": recipient, "branches": [{"site_id": r[0], "business_name": r[1]} for r in rows]}


@router.get("/api/retail/notifications")
async def retailer_notifications(limit: int = 50, authorization: str | None = Header(default=None)):
    recipient = _bearer(authorization)
    limit = max(1, min(limit, 100))
    conn = _db()
    try:
        rows = _q(conn, """SELECT n.notification_id,n.site_id,c.business_name,n.exception_type,
            n.occurred_at,n.summary,n.created_at,d.status
            FROM nexusai_retail_notifications n
            JOIN nexusai_retail_recipient_sites s ON s.site_id=n.site_id AND s.recipient_id=?
            JOIN nexusai_customers c ON c.site_id=n.site_id
            JOIN nexusai_retail_notification_deliveries d
              ON d.notification_id=n.notification_id AND d.recipient_id=?
              AND d.channel='DASHBOARD'
            ORDER BY n.created_at DESC LIMIT ?""",
            (recipient["recipient_id"],recipient["recipient_id"],limit)).fetchall()
    finally:
        conn.close()
    return {"notifications": [{"notification_id":r[0],"site_id":r[1],"branch":r[2],
        "exception_type":r[3],"occurred_at":r[4],"summary":r[5],"created_at":r[6],"delivery_status":r[7]}
        for r in rows]}


@router.post("/api/retail/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, authorization: str | None = Header(default=None)):
    recipient = _bearer(authorization)
    conn = _db()
    try:
        result = _q(conn, """UPDATE nexusai_retail_notification_deliveries
            SET status='READ',updated_at=? WHERE notification_id=? AND recipient_id=? AND channel='DASHBOARD'
            AND EXISTS (SELECT 1 FROM nexusai_retail_notifications n
                JOIN nexusai_retail_recipient_sites s ON s.site_id=n.site_id
                WHERE n.notification_id=? AND s.recipient_id=?)""",
            (time.time(),notification_id,recipient["recipient_id"],notification_id,recipient["recipient_id"]))
        conn.commit()
        if not result.rowcount:
            raise HTTPException(404, "Notification not found for your assigned branches.")
    finally:
        conn.close()
    return {"updated": True, "status": "READ"}


@router.get("/api/retail/push/config")
async def retailer_push_config():
    public_key = __import__("os").getenv("NEXUSAI_VAPID_PUBLIC_KEY", "").strip()
    private_key = __import__("os").getenv("NEXUSAI_VAPID_PRIVATE_KEY", "").strip()
    subject = __import__("os").getenv("NEXUSAI_VAPID_SUBJECT", "").strip()
    return {"configured": bool(public_key and private_key and subject), "public_key": public_key if public_key and private_key and subject else None}


@router.post("/api/retail/push/subscribe")
async def retailer_push_subscribe(payload: PushSubscriptionIn, authorization: str | None = Header(default=None)):
    recipient = _bearer(authorization)
    sub = payload.subscription
    endpoint = str(sub.get("endpoint") or "").strip()
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
        allowed_host = (host == "fcm.googleapis.com" or host == "web.push.apple.com"
            or host.endswith(".push.services.mozilla.com") or host.endswith(".notify.windows.com"))
        valid_endpoint = (parsed.scheme == "https" and parsed.netloc.lower() == host
            and allowed_host and bool(parsed.path))
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise HTTPException(422, "The browser push endpoint is not from a supported push service.")
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    if not endpoint.startswith("https://") or len(endpoint) > 2048 or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(422, "A valid browser push subscription is required.")
    encoded = json.dumps(sub, separators=(",", ":"), ensure_ascii=True)
    conn = _db()
    try:
        existing = _q(conn, "SELECT recipient_id,revoked_at FROM nexusai_retail_push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if existing and existing[0] != recipient["recipient_id"] and existing[1] is None:
            raise HTTPException(409, "This device is already registered to another retailer account. Sign out there first.")
        now = time.time()
        if existing:
            _q(conn, """UPDATE nexusai_retail_push_subscriptions SET recipient_id=?,subscription_json=?,updated_at=?,revoked_at=NULL,expires_at=(SELECT MAX(expires_at) FROM nexusai_retail_notification_tokens WHERE recipient_id=? AND revoked_at IS NULL)
                WHERE endpoint=?""", (recipient["recipient_id"],encoded,now,recipient["recipient_id"],endpoint))
        else:
            _q(conn, """INSERT INTO nexusai_retail_push_subscriptions
                (subscription_id,recipient_id,endpoint,subscription_json,created_at,updated_at,revoked_at,expires_at)
                VALUES (?,?,?,?,?,?,NULL,(SELECT MAX(expires_at) FROM nexusai_retail_notification_tokens WHERE recipient_id=? AND revoked_at IS NULL))""",
                ("rps-"+uuid.uuid4().hex,recipient["recipient_id"],endpoint,encoded,now,now,recipient["recipient_id"]))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"registered": True, "message": "This device can receive NexusAI retail alerts while the dashboard is closed."}


@router.delete("/api/retail/push/subscribe")
async def retailer_push_unsubscribe(payload: PushUnsubscribeIn, authorization: str | None = Header(default=None)):
    recipient = _bearer(authorization)
    conn = _db()
    try:
        _q(conn, """UPDATE nexusai_retail_push_subscriptions SET revoked_at=?,updated_at=?
            WHERE recipient_id=? AND endpoint=? AND revoked_at IS NULL""",
           (time.time(),time.time(),recipient["recipient_id"],payload.endpoint))
        conn.commit()
    finally:
        conn.close()
    return {"revoked": True}


def _send_push(subscription: dict, notification_id: str, branch: str):
    import os
    try:
        from pywebpush import webpush
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title":"NexusAI Retail Alert",
                "body":f"A POS exception at {branch} needs authorised human review.",
                "tag":"nexusai-retail-"+notification_id,
                "url":"/app/retail.html"},separators=(",",":")),
            vapid_private_key=os.getenv("NEXUSAI_VAPID_PRIVATE_KEY","").strip(),
            vapid_claims={"sub":os.getenv("NEXUSAI_VAPID_SUBJECT","").strip()},
            ttl=300,
            headers={"Urgency":"high"},
        )
    except Exception as exc:
        message=str(exc)
        if "410" in message or "404" in message or "410 Gone" in message or "404 Not Found" in message:
            endpoint=str(subscription.get("endpoint") or "")
            conn=_db()
            try:
                _q(conn,"UPDATE nexusai_retail_push_subscriptions SET revoked_at=?,updated_at=? WHERE endpoint=?",
                   (time.time(),time.time(),endpoint))
                conn.commit()
            finally:
                conn.close()
        _LOG.warning("Retail push delivery failed for notification %s: %s",notification_id,message[:240])


def create_for_pos_exception(site_id: str, exception_id: int | None, source_event_id: str | None, flagged: bool):
    """Create dashboard records and send best-effort browser push after POS commit."""
    if not flagged or not exception_id or not source_event_id:
        return
    conn = _db()
    push_jobs = []
    try:
        org = _q(conn, "SELECT organisation_id FROM nexusai_retail_organisation_sites WHERE site_id=?", (site_id,)).fetchone()
        if not org:
            return
        event = _q(conn, "SELECT exception_type,occurred_at FROM nexusai_pos_exceptions WHERE id=? AND site_id=?",
                   (exception_id,site_id)).fetchone()
        if not event:
            return
        notification_id = "rtn-" + uuid.uuid4().hex
        now = time.time()
        inserted = _q(conn, """INSERT INTO nexusai_retail_notifications
            (notification_id,organisation_id,site_id,pos_exception_id,source_event_id,exception_type,occurred_at,summary,created_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(site_id,source_event_id) DO NOTHING""",
            (notification_id,org[0],site_id,exception_id,source_event_id,event[0],event[1],
             "POS exception requires authorised human review.",now))
        if inserted.rowcount:
            recipients = _q(conn, """SELECT r.recipient_id,c.business_name FROM nexusai_retail_notification_recipients r
                JOIN nexusai_retail_recipient_sites s ON s.recipient_id=r.recipient_id
                JOIN nexusai_customers c ON c.site_id=s.site_id
                WHERE s.site_id=? AND r.active=TRUE AND r.dashboard_opt_in=TRUE""", (site_id,)).fetchall()
            for row in recipients:
                _q(conn, """INSERT INTO nexusai_retail_notification_deliveries
                    (delivery_id,notification_id,recipient_id,channel,status,attempt_count,created_at,updated_at)
                    VALUES (?,?,?,'DASHBOARD','PENDING',0,?,?)
                    ON CONFLICT(notification_id,recipient_id,channel) DO NOTHING""",
                   ("rtd-"+uuid.uuid4().hex,notification_id,row[0],now,now))
                subs = _q(conn, """SELECT subscription_json FROM nexusai_retail_push_subscriptions
                    WHERE recipient_id=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)""", (row[0],now)).fetchall()
                for sub in subs:
                    try:
                        push_jobs.append((json.loads(sub[0]),notification_id,row[1]))
                    except (TypeError,ValueError):
                        _LOG.warning("Skipping malformed retail push subscription for recipient %s",row[0])
        conn.commit()
    except Exception:
        conn.rollback()
        _LOG.exception("POS event persisted; dashboard notification creation will need retry.")
    finally:
        conn.close()
    if push_jobs:
        import os
        if not (os.getenv("NEXUSAI_VAPID_PUBLIC_KEY","").strip() and os.getenv("NEXUSAI_VAPID_PRIVATE_KEY","").strip() and os.getenv("NEXUSAI_VAPID_SUBJECT","").strip()):
            _LOG.warning("Retail browser push is not configured; dashboard notifications remain available.")
            return
        for subscription, notification_id, branch in push_jobs:
            _PUSH_EXECUTOR.submit(_send_push,subscription,notification_id,branch)
