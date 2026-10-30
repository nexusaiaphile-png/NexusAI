"""WhatsApp delivery adapter for NexusAI Retail Intelligence alerts.

This module formats validated POS exceptions and sends them through Twilio's
WhatsApp Business API. It does not own ingestion, video storage, or retry queues.
Credentials are read from environment variables and must never be committed.
"""
from __future__ import annotations

import os
import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from .models import POSExceptionEventIn

TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
ALLOWED_EXCEPTION_LABELS = {
    "void": "Void",
    "transaction_void": "Void",
    "refund": "Refund",
    "return": "Return",
    "return_issued": "Return",
    "no_sale": "No-Sale",
    "line_item_cancelled": "Line-Item Cancelled",
    "drawer_open": "Drawer Open",
    "manager_override": "Manager Override",
    "manual_discount": "Manual Discount",
}


class WhatsAppDeliveryError(RuntimeError):
    """Raised when WhatsApp delivery cannot be completed."""


def _https_clip_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("video_clip_url must be a credential-free HTTPS URL.")
    return value.strip()


def format_retail_exception_message(
    event: POSExceptionEventIn,
    *,
    site_name: str,
    video_clip_url: str,
) -> str:
    """Render a concise WhatsApp alert from a validated POS event and clip link."""
    site_name = site_name.strip()
    if not site_name:
        raise ValueError("site_name is required for a human-readable alert.")
    clip_url = _https_clip_url(video_clip_url)
    if event.currency != "ZAR":
        raise ValueError("Retail WhatsApp exception alerts currently require ZAR events.")

    exception_label = ALLOWED_EXCEPTION_LABELS.get(event.exception_type)
    if exception_label is None:
        raise ValueError(f"Unsupported POS exception type: {event.exception_type}")

    cashier = (event.cashier_name or "Unknown").strip() or "Unknown"
    value = Decimal(event.transaction_value)
    amount = f"R{value:,.2f}"

    return (
        "🚨 NEXUSAI RETAIL EXCEPTION ALERT 🚨\n"
        f"🏬 Store: {site_name}\n"
        f"🧾 Till: {event.till_id}\n"
        f"👤 Cashier: {cashier}\n"
        f"⚠️ Exception: {exception_label}\n"
        f"💰 Value: {amount} ZAR\n"
        f"🎥 Review footage: {clip_url}"
    )


def send_whatsapp_alert(
    event: POSExceptionEventIn,
    *,
    site_name: str,
    video_clip_url: str,
    recipient: str,
    client: httpx.Client | None = None,
) -> str:
    """Send one WhatsApp message using Twilio; return the provider message SID.

    Required environment variables:
      TWILIO_ACCOUNT_SID
      TWILIO_AUTH_TOKEN
      TWILIO_WHATSAPP_FROM  (e.g. whatsapp:+14155238886 for a configured sender)

    recipient must be an E.164 number prefixed with 'whatsapp:' or a plain
    E.164 number (the latter is prefixed here). Production sender approval,
    WhatsApp opt-in, and applicable template rules remain provider requirements.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    if not account_sid or not auth_token or not sender:
        raise WhatsAppDeliveryError(
            "WhatsApp delivery is not configured; set the Twilio account SID, "
            "auth token, and WhatsApp sender in the deployment environment."
        )

    recipient_number = recipient.strip()
    if recipient_number.startswith("whatsapp:"):
        recipient_number = recipient_number[len("whatsapp:"):]
    if not re.fullmatch(r"\+[1-9]\d{7,14}", recipient_number):
        raise ValueError("recipient must be an E.164 phone number.")
    if not sender.startswith("whatsapp:+"):
        raise WhatsAppDeliveryError("TWILIO_WHATSAPP_FROM must use the whatsapp:+E.164 format.")

    body = format_retail_exception_message(
        event, site_name=site_name, video_clip_url=video_clip_url
    )
    payload = {
        "From": sender,
        "To": f"whatsapp:{recipient_number}",
        "Body": body,
    }
    url = TWILIO_MESSAGES_URL.format(account_sid=account_sid)
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
    try:
        response = http.post(url, data=payload, auth=(account_sid, auth_token))
        response.raise_for_status()
        try:
            message_sid = response.json().get("sid")
        except ValueError as exc:
            raise WhatsAppDeliveryError("Twilio returned a non-JSON response.") from exc
        if not isinstance(message_sid, str) or not message_sid:
            raise WhatsAppDeliveryError("Twilio response did not include a message SID.")
        return message_sid
    except httpx.HTTPError as exc:
        # Do not include request headers or credentials in raised errors.
        raise WhatsAppDeliveryError(
            f"WhatsApp provider request failed ({type(exc).__name__})."
        ) from exc
    finally:
        if owns_client:
            http.close()
