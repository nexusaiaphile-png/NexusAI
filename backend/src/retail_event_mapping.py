"""Deterministic retail event mapping for NexusAI alarm pipelines.

Only explicit semantic alarm labels or operator-configured vendor event codes are
promoted to theft/weapon/threat. Generic motion and line-crossing are not proof
of a crime and are left unchanged unless their specific vendor code is configured.
"""
from __future__ import annotations

import os
import re
from typing import Any


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _codes(env_name: str) -> set[str]:
    return {_norm(v) for v in os.getenv(env_name, "").split(",") if _norm(v)}


def _signals(event: dict[str, Any]) -> set[str]:
    signals: set[str] = set()
    keys = (
        "event", "event_type", "eventType", "eventTypeName", "eventTypeCode",
        "description", "eventDescription", "alarmType", "alarmTypeName",
        "eventCode", "alarmCode", "hikconnect_event_type",
    )
    for key in keys:
        value = event.get(key)
        if value not in (None, ""):
            signals.add(_norm(value))
    for container_key in ("hpp_data", "hikconnect_data"):
        raw = event.get(container_key)
        if not isinstance(raw, dict):
            continue
        nested = raw.get("EventNotificationAlert")
        data = nested if isinstance(nested, dict) else raw
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                signals.add(_norm(value))
    return signals


_RULES = (
    ("weapon", {
        "weapon", "weapon_detected", "weapon_detection", "firearm_detected",
        "gun_detected", "knife_detected", "weapon_alarm",
    }, "NEXUSAI_RETAIL_WEAPON_EVENT_CODES", "CRITICAL"),
    ("threat", {
        "panic_button", "panic_button_pressed", "duress_alarm", "emergency_panic",
        "staff_panic_alarm", "physical_assault", "violence_detected",
        "violent_behavior",
    }, "NEXUSAI_RETAIL_THREAT_EVENT_CODES", "CRITICAL"),
    ("theft", {
        "theft", "theft_detected", "shoplifting", "shoplifting_detected",
        "retail_zone_intrusion", "restricted_retail_zone_intrusion",
        "after_hours_intrusion", "off_hours_intrusion",
    }, "NEXUSAI_RETAIL_THEFT_EVENT_CODES", "HIGH"),
)

_SEVERITY_RANK = {
    "INFO": 0, "LOW": 1, "MEDIUM": 2, "WARNING": 2,
    "HIGH": 3, "CRITICAL": 4,
}


def normalize_retail_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one event, returning None when it is not a core retail alert.

    Configure comma-separated, exact vendor alarm codes on the server:
      NEXUSAI_RETAIL_THEFT_EVENT_CODES
      NEXUSAI_RETAIL_WEAPON_EVENT_CODES
      NEXUSAI_RETAIL_THREAT_EVENT_CODES

    A generic line-crossing/motion code is deliberately not classified as a crime
    unless its exact code is configured after validating its site-specific meaning.
    """
    if not isinstance(event, dict):
        raise TypeError("Retail event mapper expects a dictionary.")
    result = dict(event)
    original = str(result.get("event") or "HIKVISION_ALARM")
    signals = _signals(result)

    for label, explicit, env_name, required_severity in _RULES:
        configured = _codes(env_name)
        matched = sorted(signals.intersection(explicit | configured))
        if not matched:
            continue
        signal = matched[0]
        result.setdefault("source_event", original)
        result["event"] = label
        result["retail_intelligence"] = {
            "label": label,
            "rule": "configured_vendor_code" if signal in configured else "explicit_event_semantics",
            "matched_signal": signal,
            "original_event": original,
            "version": "1.0",
        }
        current = str(result.get("severity") or "INFO").upper()
        if _SEVERITY_RANK.get(current, 0) < _SEVERITY_RANK[required_severity]:
            result["severity"] = required_severity
        else:
            result["severity"] = current
        return result

    # Strict retail-only mode: unmatched hardware/security events are discarded.
    # Returning None lets callers stop before persistence, history, or notifications.
    return None
