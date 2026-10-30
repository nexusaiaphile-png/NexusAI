from decimal import Decimal

import httpx
import pytest

from backend.src.models import POSExceptionEventIn
from backend.src.whatsapp_alerts import (
    WhatsAppDeliveryError,
    format_retail_exception_message,
    send_whatsapp_alert,
)


def valid_event(**overrides):
    data = {
        "site_id": "spar-site-001",
        "source_event_id": "pos-evt-0001",
        "till_id": "lane-04",
        "exception_type": "refund",
        "transaction_value": "1250.50",
        "currency": "ZAR",
        "occurred_at": "2026-10-19T14:30:00+02:00",
        "cashier_name": "Sam Dlamini",
    }
    data.update(overrides)
    return POSExceptionEventIn.model_validate(data)


def test_formats_whatsapp_retail_exception_card():
    message = format_retail_exception_message(
        valid_event(),
        site_name="Bergville Main Store",
        video_clip_url="https://video.example.com/clips/secure-clip-123",
    )
    assert message.startswith("🚨 NEXUSAI RETAIL EXCEPTION ALERT 🚨")
    assert "Store: Bergville Main Store" in message
    assert "Till: lane-04" in message
    assert "Cashier: Sam Dlamini" in message
    assert "Exception: Refund" in message
    assert "Value: R1,250.50 ZAR" in message
    assert "https://video.example.com/clips/secure-clip-123" in message


@pytest.mark.parametrize("url", [
    "http://video.example.com/clip",
    "javascript:alert(1)",
    "https://user:pass@video.example.com/clip",
])
def test_rejects_unsafe_video_url(url):
    with pytest.raises(ValueError):
        format_retail_exception_message(
            valid_event(), site_name="Test Store", video_clip_url=url
        )


def test_requires_zar_for_current_alert_template():
    with pytest.raises(ValueError, match="require ZAR"):
        format_retail_exception_message(
            valid_event(currency="USD"),
            site_name="Test Store",
            video_clip_url="https://video.example.com/clip",
        )


def test_send_uses_twilio_and_returns_message_sid(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"sid": "SM-test-message"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sid = send_whatsapp_alert(
            valid_event(),
            site_name="Test Store",
            video_clip_url="https://video.example.com/clip",
            recipient="+27821234567",
            client=client,
        )

    assert sid == "SM-test-message"
    assert requests[0].url.host == "api.twilio.com"
    assert requests[0].url.scheme == "https"
    assert requests[0].read().find(b"whatsapp%3A%2B27821234567") >= 0 or b"whatsapp%3A%2B27821234567" in requests[0].read()


def test_send_fails_closed_without_provider_configuration(monkeypatch):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(WhatsAppDeliveryError, match="not configured"):
        send_whatsapp_alert(
            valid_event(),
            site_name="Test Store",
            video_clip_url="https://video.example.com/clip",
            recipient="+27821234567",
        )
