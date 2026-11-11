"""Disable camera-only security classifications for NexusAI Retail Intelligence.

Hikvision events are retained only as timing/evidence signals for POS correlation.
NexusAI does not classify theft, weapons, threats, intrusion, or generic camera
activity as retail exceptions. POS events remain the source of truth.
"""
from __future__ import annotations

from typing import Any


def normalize_retail_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return None for every camera-only event.

    HPP events are correlated with stored POS exceptions by the HPP event sink
    before this function is called. Camera events must never become standalone
    security alerts in the Retail Intelligence product.
    """
    if not isinstance(event, dict):
        raise TypeError("Retail event mapper expects a dictionary.")
    return None
