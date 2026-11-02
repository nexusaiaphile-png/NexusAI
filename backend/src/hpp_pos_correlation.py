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


def _picture_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("pictureList")
    if raw is None:
        raw = payload.get("picturelist")
    if isinstance(raw, dict):
        raw = [raw]
    items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if items:
        return items
    for key in ("filePath", "pictureUrl", "picUrl", "picturePath", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return [{"url": value.strip()}]
    return []


def _picture_reference(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = _alarm_payload(event)
    for item in _picture_items(payload):
        value = item.get("url") or item.get("filePath") or item.get("pictureUrl")
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            parsed = urlsplit(candidate)
            if parsed.scheme == "https" and parsed.hostname:
                return {"file_path": "", "url": candidate, "encrypt": item.get("encrypt")}
            if not parsed.scheme and not candidate.startswith("//"):
                return {"file_path": candidate, "url": "", "encrypt": item.get("encrypt")}
    return None


def _snapshot_details(response: dict[str, Any], fallback_encrypt: Any = None) -> tuple[str, Any]:
    data = response.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    candidates: list[Any] = [data.get(key) for key in
                             ("url", "pictureUrl", "picUrl", "fileUrl", "downloadUrl")]
    for nested_key in ("list", "items"):
        items = data.get(nested_key)
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    candidates.extend(item.get(k) for k in
                                      ("url", "pictureUrl", "picUrl", "fileUrl"))
                    if item.get("encrypt") is not None:
                        fallback_encrypt = item.get("encrypt")
    encrypt = data.get("encrypt", fallback_encrypt)
    for value in candidates:
        if isinstance(value, str) and value.strip():
            parsed = urlsplit(value.strip())
            if parsed.scheme == "https" and parsed.hostname:
                return value.strip(), encrypt
    return "", encrypt


def _snapshot_url(response: dict[str, Any]) -> str:
    """Compatibility helper used by tests/callers needing only the media URL."""
    return _snapshot_details(response)[0]


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
        if camera != device_serial:
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
                           happened_at: datetime, encrypt: Any = 0) -> None:
    # Production has no snapshot_url column. Store the still-image URL and
    # vendor metadata in source_ref_id; never mislabel it as a video clip.
    source_ref = json.dumps({
        "provider": "hik-partner-pro",
        "device_serial": device_serial,
        "file_path": file_path,
        "happen_time": happened_at.isoformat(),
        "snapshot_url": snapshot_url,
        "encrypt": encrypt,
        "previewable": str(encrypt if encrypt is not None else 0) == "0",
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

    picture = _picture_reference(event)
    if not picture:
        return {
            "status": "matched_without_snapshot",
            "exception_id": exception["id"],
            "reason": "HPP alarm did not include a supported pictureList URL",
        }

    file_path = picture["file_path"]
    snapshot_url = picture["url"]
    encrypt = picture.get("encrypt", 0)
    if file_path:
        # Only ISAPI_FILES-style paths require HPP URL resolution. Do not send
        # arbitrary relative paths to the pictureurl API.
        if not file_path.startswith("ISAPI_FILES/"):
            return {
                "status": "matched_without_snapshot",
                "exception_id": exception["id"],
                "reason": "Unsupported HPP picture path format",
            }
        try:
            from backend.src.hpp_integration import picture_url
            response = picture_url(file_path)
        except Exception as exc:
            # Snapshot retrieval is optional evidence; preserve the POS match
            # without blocking or repeatedly redelivering the whole HPP batch.
            LOG.warning("HPP picture URL lookup failed for matched exception %s: %s",
                        exception["id"], exc)
            return {
                "status": "matched_without_snapshot",
                "exception_id": exception["id"],
                "reason": "HPP picture URL lookup failed",
            }
        snapshot_url, encrypt = _snapshot_details(response, encrypt)

    if not snapshot_url:
        return {
            "status": "matched_without_snapshot",
            "exception_id": exception["id"],
            "reason": "HPP picture payload contained no valid HTTPS media URL",
        }

    _persist_snapshot_link(exception, snapshot_url, file_path, device_serial,
                           happened_at, encrypt)
    encrypted = str(encrypt if encrypt is not None else 0) in {"1", "2"}
    return {
        "status": "matched_encrypted_snapshot_persisted" if encrypted else "matched_snapshot_persisted",
        "previewable": not encrypted,
        "encrypt": encrypt,
        "exception_id": exception["id"],
        "window_seconds": MATCH_WINDOW_SECONDS,
    }
