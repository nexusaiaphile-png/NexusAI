"""Correlate HPP cloud alarms with POS exceptions and persist snapshot references.

This module is called from the existing HPP message-queue consumer. It does not
create a second poller, acknowledge HPP batches, or alter the POS ingestion API.
PostgreSQL is the production source of truth; no local SQLite fallback is used.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

LOG = logging.getLogger("nexusai.hpp_pos_correlation")
MATCH_WINDOW_SECONDS = max(1, min(30, int(os.getenv("NEXUSAI_HPP_POS_MATCH_WINDOW_SECONDS", "5"))))


class CorrelationError(RuntimeError):
    """A retryable error while correlating or persisting HPP evidence."""


def _parse_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        raise ValueError("HPP event has no happenTime/triggerTime timestamp")
    if isinstance(value, (int, float)):
        # HPP timestamps may be epoch seconds or milliseconds.
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    raw = str(value).strip()
    if raw.isdigit():
        return _parse_timestamp(int(raw))
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("HPP event timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _alarm_payload(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("hpp_data") or {}
    if not isinstance(data, dict):
        return {}
    nested = data.get("EventNotificationAlert")
    return nested if isinstance(nested, dict) else data


def _file_path(event: dict[str, Any]) -> str:
    payload = _alarm_payload(event)
    for key in ("filePath", "pictureUrl", "picUrl", "picturePath", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            # The HPP pictureurl endpoint expects the vendor filePath, not an
            # arbitrary remote URL. Only accept a path-like vendor reference.
            candidate = value.strip()
            parsed = urlsplit(candidate)
            if not parsed.scheme and not candidate.startswith("//"):
                return candidate
    return ""


def _snapshot_url(response: dict[str, Any]) -> str:
    data = response.get("data") or {}
    candidates: list[Any] = []
    if isinstance(data, dict):
        for key in ("url", "pictureUrl", "picUrl", "fileUrl", "downloadUrl"):
            candidates.append(data.get(key))
        for nested_key in ("list", "items"):
            items = data.get(nested_key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        candidates.extend(item.get(k) for k in ("url", "pictureUrl", "picUrl", "fileUrl"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            parsed = urlsplit(value.strip())
            if parsed.scheme == "https" and parsed.hostname:
                return value.strip()
    return ""


def _connect():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise CorrelationError("DATABASE_URL is required for HPP/POS correlation")
    try:
        import psycopg
    except ImportError as exc:
        raise CorrelationError("psycopg is required for PostgreSQL HPP/POS correlation") from exc
    return psycopg.connect(database_url, connect_timeout=10)


def _find_pos_exception(site_id: str, device_serial: str, happened_at: datetime) -> dict[str, Any] | None:
    start = happened_at - timedelta(seconds=MATCH_WINDOW_SECONDS)
    end = happened_at + timedelta(seconds=MATCH_WINDOW_SECONDS)
    # camera_id is the existing POS exception mapping field. HPP channels are
    # normalised as '<deviceSerial>-<channel>'; accept exact serial or that prefix.
    sql = """
        SELECT id, site_id, till_id, exception_type, occurred_at, camera_id,
               source_event_id, transaction_id
          FROM nexusai_pos_exceptions
         WHERE site_id = %s
           AND flagged_for_review = TRUE
           AND camera_id IS NOT NULL
           AND (camera_id = %s OR camera_id LIKE %s)
           AND occurred_at::timestamptz BETWEEN %s AND %s
         ORDER BY ABS(EXTRACT(EPOCH FROM (occurred_at::timestamptz - %s))) ASC
         LIMIT 1
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    site_id, device_serial, device_serial + "-%",
                    start, end, happened_at,
                ))
                row = cur.fetchone()
    except Exception as exc:
        raise CorrelationError("Could not query the POS exception history") from exc
    if not row:
        return None
    keys = ("id", "site_id", "till_id", "exception_type", "occurred_at",
            "camera_id", "source_event_id", "transaction_id")
    return dict(zip(keys, row))


def _persist_snapshot_link(exception: dict[str, Any], snapshot_url: str,
                           file_path: str, device_serial: str,
                           happened_at: datetime) -> None:
    # snapshot_url is deliberately stored separately from video_clip_url. An
    # alarm picture is not a recorded playback clip and must not be labelled as one.
    sql = """
        INSERT INTO nexusai_pos_exception_video_links
            (exception_id, site_id, camera_id, source_ref_id, match_status,
             created_at, snapshot_url)
        VALUES (%s, %s, %s, %s, %s, EXTRACT(EPOCH FROM NOW()), %s)
        ON CONFLICT (exception_id) DO UPDATE SET
            camera_id = EXCLUDED.camera_id,
            source_ref_id = EXCLUDED.source_ref_id,
            match_status = EXCLUDED.match_status,
            snapshot_url = EXCLUDED.snapshot_url,
            created_at = EXCLUDED.created_at
    """
    source_ref = json.dumps({
        "provider": "hik-partner-pro",
        "device_serial": device_serial,
        "file_path": file_path,
        "happen_time": happened_at.isoformat(),
    }, separators=(",", ":"))
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    exception["id"], exception["site_id"],
                    exception.get("camera_id"), source_ref,
                    "matched_hpp_snapshot", snapshot_url,
                ))
    except Exception as exc:
        raise CorrelationError("Could not persist the HPP snapshot reference") from exc


def correlate_hpp_event(event: dict[str, Any]) -> dict[str, Any]:
    """Match one mapped HPP alarm to a POS exception and save its snapshot URL.

    A no-match or missing picture path is a normal outcome. API/DB errors raise
    CorrelationError so the existing HPP batch remains unacknowledged for retry.
    """
    site_id = str(event.get("site_id") or "").strip()
    device_serial = str(event.get("hpp_device_serial") or "").strip()
    if not site_id or not device_serial:
        return {"status": "skipped", "reason": "missing_site_or_device_mapping"}

    payload = _alarm_payload(event)
    happened_value = (
        payload.get("happenTime") or payload.get("triggerTime")
        or payload.get("dateTime") or payload.get("timestamp")
        or event.get("timestamp")
    )
    try:
        happened_at = _parse_timestamp(happened_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CorrelationError("HPP alarm timestamp is invalid") from exc

    exception = _find_pos_exception(site_id, device_serial, happened_at)
    if not exception:
        return {"status": "no_pos_match", "window_seconds": MATCH_WINDOW_SECONDS}

    file_path = _file_path(event)
    if not file_path:
        return {
            "status": "matched_without_snapshot",
            "exception_id": exception["id"],
            "reason": "HPP alarm did not include a picture filePath",
        }

    try:
        from hpp_integration import picture_url
        response = picture_url(file_path)
    except Exception as exc:
        raise CorrelationError("HPP picture URL request failed") from exc

    snapshot_url = _snapshot_url(response)
    if not snapshot_url:
        raise CorrelationError("HPP picture URL response contained no HTTPS media URL")

    _persist_snapshot_link(exception, snapshot_url, file_path, device_serial, happened_at)
    return {
        "status": "matched_snapshot_persisted",
        "exception_id": exception["id"],
        "window_seconds": MATCH_WINDOW_SECONDS,
    }
