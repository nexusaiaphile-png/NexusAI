"""Owner-only Gmail approval workflow for NexusAI distributor applications.

Only the configured administrator mailbox is accepted as an approval sender.
Replies must be in the original notification thread and begin with exactly
APPROVED or REJECTED. Credentials are read only from environment variables.
"""
import base64
import logging
import os
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parents[2] / "nexusai.db"
ADMIN_EMAIL = "nexusai.aphile@gmail.com"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_poll_thread = None
_poll_lock = threading.Lock()
_access_token = None
_access_token_expires_at = 0.0


def _credentials():
    return {
        "client_id": os.getenv("NEXUSAI_GMAIL_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("NEXUSAI_GMAIL_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("NEXUSAI_GMAIL_REFRESH_TOKEN", "").strip(),
        "account": os.getenv("NEXUSAI_GMAIL_ACCOUNT", ADMIN_EMAIL).strip().lower(),
    }


def configured():
    c = _credentials()
    return bool(c["client_id"] and c["client_secret"] and c["refresh_token"]
                and c["account"] == ADMIN_EMAIL)


def _postgres():
    return bool(os.getenv("DATABASE_URL", "").strip())


@contextmanager
def _db():
    if _postgres():
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            yield conn
    else:
        import sqlite3
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


def init_approval_db():
    with _db() as conn:
        _sql(conn, """CREATE TABLE IF NOT EXISTS nexusai_distributor_approval_emails (
            distributor_id TEXT PRIMARY KEY,
            gmail_thread_id TEXT NOT NULL,
            notification_message_id TEXT NOT NULL,
            notified_at DOUBLE PRECISION NOT NULL,
            processed_message_id TEXT,
            last_error TEXT
        )""")
        _sql(conn, """CREATE INDEX IF NOT EXISTS idx_nexusai_distributor_approval_pending
            ON nexusai_distributor_approval_emails(processed_message_id, notified_at)""")


def _get_access_token(force=False):
    global _access_token, _access_token_expires_at
    now = time.time()
    if not force and _access_token and now < _access_token_expires_at - 60:
        return _access_token
    c = _credentials()
    if not configured():
        raise RuntimeError("NexusAI Gmail approval credentials are not configured.")
    response = httpx.post(TOKEN_URL, data={
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "refresh_token": c["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=20)
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Google did not return a Gmail access token.")
    _access_token = token
    _access_token_expires_at = now + int(data.get("expires_in", 3600))
    return token


def _gmail(method, path, *, token, params=None, json_body=None):
    response = httpx.request(
        method, f"{GMAIL_API}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        params=params, json=json_body, timeout=25,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def notify_distributor_application(distributor_id, full_name, company_name, email, country):
    """Send an owner notification and persist its Gmail thread for reply matching."""
    if not configured():
        logger.warning("Distributor email notification skipped: Gmail credentials are not configured.")
        return False
    try:
        message = EmailMessage()
        message["To"] = ADMIN_EMAIL
        message["From"] = ADMIN_EMAIL
        message["Subject"] = f"NEXUSAI DISTRIBUTOR APPROVAL [{distributor_id}]"
        message.set_content(
            "A new NexusAI Global Distributor application has been submitted.\n\n"
            f"Applicant: {full_name}\nCompany: {company_name}\nEmail: {email}\nCountry: {country}\n"
            f"Application reference: {distributor_id}\n\n"
            "To review this application, reply to this email with exactly one command on the first line:\n"
            "APPROVED\nor\nREJECTED\n\n"
            "Only replies from the authorised NexusAI administrator mailbox are processed. "
            "The application reference in this email thread is required."
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        token = _get_access_token()
        sent = _gmail("POST", "messages/send", token=token, json_body={"raw": raw})
        thread_id = str(sent.get("threadId", ""))
        message_id = str(sent.get("id", ""))
        if not thread_id or not message_id:
            raise RuntimeError("Gmail accepted no usable message/thread identifier.")
        with _db() as conn:
            _sql(conn, """INSERT INTO nexusai_distributor_approval_emails
                (distributor_id,gmail_thread_id,notification_message_id,notified_at)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(distributor_id) DO UPDATE SET
                gmail_thread_id=excluded.gmail_thread_id,
                notification_message_id=excluded.notification_message_id,
                notified_at=excluded.notified_at,
                processed_message_id=NULL,last_error=NULL""",
                (distributor_id, thread_id, message_id, time.time()))
        logger.info("Distributor approval notification sent for %s.", distributor_id)
        return True
    except Exception as exc:
        logger.exception("Could not send distributor approval notification for %s (%s).",
                         distributor_id, type(exc).__name__)
        return False


def _decode_part(part):
    body = (part or {}).get("body") or {}
    data = body.get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _plain_text(payload):
    if not isinstance(payload, dict):
        return ""
    chunks = []
    if payload.get("mimeType") == "text/plain":
        chunks.append(_decode_part(payload))
    for part in payload.get("parts") or []:
        chunks.append(_plain_text(part))
    return "\n".join(chunk for chunk in chunks if chunk)


def _reply_command(message):
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in ((message.get("payload") or {}).get("headers") or [])
    }
    sender = parseaddr(headers.get("from", ""))[1].strip().lower()
    if sender != ADMIN_EMAIL:
        return None
    if "SENT" not in (message.get("labelIds") or []):
        return None
    body = _plain_text(message.get("payload") or {})
    for line in body.splitlines():
        candidate = line.strip().upper()
        if not candidate:
            continue
        return candidate if candidate in {"APPROVED", "REJECTED"} else None
    return None


def _process_thread(token, row):
    distributor_id, thread_id, notification_id, notified_at = row
    thread = _gmail("GET", f"threads/{thread_id}", token=token, params={"format": "full"})
    messages = thread.get("messages") or []
    candidates = []
    for message in messages:
        message_id = str(message.get("id", ""))
        if not message_id or message_id == notification_id:
            continue
        try:
            sent_at = int(message.get("internalDate", "0")) / 1000
        except (TypeError, ValueError):
            continue
        if sent_at <= float(notified_at):
            continue
        command = _reply_command(message)
        if command:
            candidates.append((sent_at, message_id, command))
    if not candidates:
        return
    _, message_id, command = max(candidates, key=lambda item: item[0])
    new_status = "ACTIVE" if command == "APPROVED" else "REJECTED"
    with _db() as conn:
        changed = _sql(conn, """UPDATE nexusai_distributor_accounts SET status=%s
            WHERE distributor_id=%s AND status='PENDING'""",
            (new_status, distributor_id))
        _sql(conn, """UPDATE nexusai_distributor_approval_emails
            SET processed_message_id=%s,last_error=NULL WHERE distributor_id=%s
            AND processed_message_id IS NULL""", (message_id, distributor_id))
    if changed.rowcount == 1:
        logger.info("Distributor %s status changed to %s by owner email reply.",
                    distributor_id, new_status)


def poll_approval_replies_once():
    if not configured():
        return
    token = _get_access_token()
    with _db() as conn:
        rows = _sql(conn, """SELECT distributor_id,gmail_thread_id,notification_message_id,notified_at
            FROM nexusai_distributor_approval_emails
            WHERE processed_message_id IS NULL ORDER BY notified_at ASC LIMIT 100""").fetchall()
    for row in rows:
        try:
            _process_thread(token, row)
        except httpx.HTTPStatusError as exc:
            logger.warning("Gmail approval poll failed for %s (HTTP %s).",
                           row[0], exc.response.status_code)
        except Exception as exc:
            logger.warning("Gmail approval poll failed for %s (%s).", row[0], type(exc).__name__)


def _poll_loop():
    while True:
        try:
            poll_approval_replies_once()
        except Exception as exc:
            logger.warning("Gmail approval polling cycle failed (%s).", type(exc).__name__)
        time.sleep(45)


def start_approval_poller():
    global _poll_thread
    if not configured():
        logger.info("Gmail distributor approval polling is disabled until credentials are configured.")
        return False
    with _poll_lock:
        if _poll_thread and _poll_thread.is_alive():
            return True
        _poll_thread = threading.Thread(
            target=_poll_loop, name="nexusai-gmail-approvals", daemon=True
        )
        _poll_thread.start()
    logger.info("Gmail distributor approval polling started.")
    return True
