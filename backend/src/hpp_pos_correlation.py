"""Correlate HPP cloud alarms with POS exceptions and persist snapshot references.

This module runs inside the existing HPP message-queue consumer. It does not
create a second poller, acknowledge HPP batches, or alter POS ingestion.
The production schema is used as-is; snapshot metadata is stored in source_ref_id.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

LOG = logging.getLogger("nexusai.hpp_pos_correlation")


def _match_window_seconds() -> int:
    try:
        configured = int(os.getenv("NEXUSAI_HPP_POS_MATCH_WINDOW_SECONDS", "5"))
    except (TypeError, ValueError):
        LOG.warning("Invalid NEXUSAI_HPP_POS_MATCH_WINDOW_SECONDS; using 5 seconds")
        configured = 5
    return max(1, min(30, configured))


MATCH_WINDOW_SECONDS = _match_window_seconds()


class CorrelationError(RuntimeError):
    """A retryable error while correlating or persisting HPP evidence."""


def _parse_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        raise ValueError("Timestamp is missing")
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    raw = str(value).strip()
    if raw.isdigit():
        return _parse_timestamp(int(raw))
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
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
    # occurred_at is TEXT in production. Fetch only verified columns and parse
    # timestamps in Python so malformed legacy values cannot abort the SQL query.
    sql = """
        SELECT id, site_id, occurred_at, camera_id, source_event_id, flagged_for_review
          FROM public.nexusai_pos_exceptions
         WHERE site_id = %s
           AND flagged_for_review = TRUE
           AND camera_id IS NOT NULL
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (site_id,))
                rows = cur.fetchall()
    except Exception as exc:
        raise CorrelationError("Could not query the POS exception history") from exc

    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        row_id, row_site, occurred_raw, camera_id, source_event_id, flagged = row
        if not flagged or not camera_id:
            continue
        camera = str(camera_id).strip()
        if camera != device_serial and not camera.startswith(device_serial + "-"):
            continue
        try:
            occurred_at = _parse_timestamp(occurred_raw)
        except (TypeError, ValueError, OverflowError, OSError):
            LOG.warning("Skipping POS exception %s with invalid occurred_at", row_id)
            continue
        if start <= occurred_at <= end:
            exception = {
                "id": row_id,
                "site_id": row_site,
                "occurred_at": occurred_at,
                "camera_id": camera,
                "source_event_id": source_event_id,
            }
            candidates.append((abs((occurred_at - happened_at).total_seconds()), exception))
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _persist_snapshot_link(exception: dict[str, Any], snapshot_url: str,
                           file_path: str, device_serial: str,
                           happened_at: datetime) -> None:
    # Production has no snapshot_url column. Store the still-image URL and
    # vendor metadata in source_ref_id; never mislabel it as a video clip.
    source_ref = json.dumps({
        "provider": "hik-partner-pro",
        "device_serial": device_serial,
        "file_path": file_path,
        "happen_time": happened_at.isoformat(),
        "snapshot_url": snapshot_url,
    }, separators=(",", ":"))
    sql = """
        INSERT INTO public.nexusai_pos_exception_video_links
            (exception_id, site_id, camera_id, source_ref_id, match_status, created_at)
        VALUES (%s, %s, %s, %s, %s, EXTRACT(EPOCH FROM NOW()))
        ON CONFLICT (exception_id) DO UPDATE SET
            camera_id = EXCLUDED.camera_id,
            source_ref_id = EXCLUDED.source_ref_id,
            match_status = EXCLUDED.match_status,
            created_at = EXCLUDED.created_at
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    exception["id"], exception["site_id"], exception.get("camera_id"),
                    source_ref, "matched_hpp_snapshot",
                ))
    except Exception as exc:
        raise CorrelationError("Could not persist the HPP snapshot reference") from exc


def correlate_hpp_event(event: dict[str, Any]) -> dict[str, Any]:
    """Match one mapped HPP alarm to a POS exception and save its snapshot URL."""
    site_id = str(event.get("site_id") or "").strip()
    device_serial = str(event.get("hpp_device_serial") or "").strip()
    if not site_id or not device_serial:
        return {"status": "skipped", "reason": "missing_site_or_device_mapping"}

    payload = _alarm_payload(event)
    happened_value = (
        payload.get("happenTime") or payload.get("triggerTime")
        or payload.get("dateTime") or payload.get("timestamp")
    )
    # Do not substitute normalize_message's ingestion-time fallback: that can
    # create a false POS match when the vendor alarm has no event timestamp.
    if happened_value in (None, ""):
        return {"status": "skipped", "reason": "missing_hpp_event_timestamp"}
    try:
        happened_at = _parse_timestamp(happened_value)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
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
        from backend.src.hpp_integration import picture_url
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
