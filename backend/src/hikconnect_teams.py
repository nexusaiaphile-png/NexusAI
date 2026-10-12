"""Isolated Hik-Connect for Teams OpenAPI REST adapter.

This module is intentionally not auto-started and does not replace HPP.
Set HIKCONNECT_TEAMS_APP_KEY and HIKCONNECT_TEAMS_SECRET_KEY only after
confirming credentials and region with Hikvision. Wire an event sink before
enabling consumption in production.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

LOG = logging.getLogger("nexusai.hikconnect_teams")

TOKEN_PATH = "/api/hccgw/platform/v1/token/get"
SUBSCRIBE_PATH = "/api/hccgw/alarm/v1/mq/subscribe"
MESSAGES_PATH = "/api/hccgw/alarm/v1/mq/messages"
COMPLETE_PATH = "/api/hccgw/alarm/v1/mq/messages/complete"
MAX_REQUESTS_PER_SECOND = 5
DEFAULT_MAX_MESSAGES = 10
MAX_MESSAGES = 100
POLL_INTERVAL_SECONDS = 0.5

class HikConnectTeamsError(RuntimeError):
    pass

class HikConnectTeamsClient:
    """Small synchronous client with token caching and a process-local rate gate."""

    def __init__(self, app_key: str | None = None, secret_key: str | None = None):
        self.app_key = (app_key if app_key is not None else os.getenv("HIKCONNECT_TEAMS_APP_KEY", "")).strip()
        self.secret_key = (secret_key if secret_key is not None else os.getenv("HIKCONNECT_TEAMS_SECRET_KEY", "")).strip()
        self._token: str = ""
        self._token_expiry = 0.0
        self._area_domain = ""
        self._lock = threading.RLock()
        self._request_times: list[float] = []

    def configured(self) -> bool:
        return bool(self.app_key and self.secret_key)

    def _rate_limit(self) -> None:
        # At most five starts in any rolling one-second window per process.
        while True:
            with self._lock:
                now = time.monotonic()
                self._request_times = [t for t in self._request_times if now - t < 1.0]
                if len(self._request_times) < MAX_REQUESTS_PER_SECOND:
                    self._request_times.append(now)
                    return
                wait_for = 1.0 - (now - self._request_times[0])
            time.sleep(max(0.01, wait_for))

    def _post(self, base: str, path: str, body: dict[str, Any], token: str | None = None,
              timeout: int = 20) -> dict[str, Any]:
        if not base.startswith("https://"):
            raise HikConnectTeamsError("Hik-Connect API base must use HTTPS.")
        self._rate_limit()
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json",
        }
        if token:
            headers["Token"] = token
        req = urllib.request.Request(
            base.rstrip("/") + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HikConnectTeamsError(f"Hik-Connect HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise HikConnectTeamsError(f"Hik-Connect connection failed: {exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HikConnectTeamsError("Hik-Connect returned a non-JSON response.") from exc
        if not isinstance(payload, dict):
            raise HikConnectTeamsError("Hik-Connect returned an invalid response object.")
        code = str(payload.get("errorCode", ""))
        if code != "0":
            # Never include credentials or request headers in errors.
            raise HikConnectTeamsError(
                f"Hik-Connect errorCode={code or 'missing'}: "
                f"{str(payload.get('message') or 'request failed')[:300]}"
            )
        return payload

    def authenticate(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not self.configured():
                raise HikConnectTeamsError(
                    "Set HIKCONNECT_TEAMS_APP_KEY and HIKCONNECT_TEAMS_SECRET_KEY on the server."
                )
            if not force and self._token and time.time() < self._token_expiry - 300 and self._area_domain:
                return {"authenticated": True, "area_domain": self._area_domain,
                        "expires_at": self._token_expiry}
            payload = self._post(
                "https://api.hikcentralconnect.com",
                TOKEN_PATH,
                {"appKey": self.app_key, "secretKey": self.secret_key},
            )
            data = payload.get("data") or {}
            token = str(data.get("accessToken") or "")
            area_domain = str(data.get("areaDomain") or "").rstrip("/")
            expiry_raw = data.get("expireTime")
            if not token or not area_domain or not area_domain.startswith("https://"):
                raise HikConnectTeamsError("Token response is missing accessToken or HTTPS areaDomain.")
            try:
                expiry_number = float(expiry_raw)
            except (TypeError, ValueError) as exc:
                raise HikConnectTeamsError("Token response has an invalid expireTime.") from exc
            # Guides may show epoch seconds; tolerate milliseconds if the value is in ms.
            self._token_expiry = expiry_number / 1000.0 if expiry_number > 10_000_000_000 else expiry_number
            self._token = token
            self._area_domain = area_domain
            return {"authenticated": True, "area_domain": area_domain, "expires_at": self._token_expiry}

    def _authorized_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        info = self.authenticate()
        with self._lock:
            token, base = self._token, self._area_domain
        try:
            return self._post(base, path, body, token=token)
        except HikConnectTeamsError as exc:
            if "errorCode=" not in str(exc):
                raise
            # Re-authenticate once for a rejected/expired token, then retry once.
            self.authenticate(force=True)
            with self._lock:
                token, base = self._token, self._area_domain
            return self._post(base, path, body, token=token)

    def subscribe(self, event_types: list[int] | None = None, subscribe_mode: int = 0) -> dict[str, Any]:
        if subscribe_mode not in (0, 1):
            raise HikConnectTeamsError("subscribe_mode must be 0 (all) or 1 (event list).")
        event_types = event_types or []
        if subscribe_mode == 1 and not event_types:
            raise HikConnectTeamsError("event_types is required when subscribe_mode=1.")
        body: dict[str, Any] = {"subscribeType": 1, "subscribeMode": subscribe_mode}
        if subscribe_mode == 1:
            body["eventType"] = event_types
        return self._authorized_post(SUBSCRIBE_PATH, body)

    def poll_messages(self, subscription_id: str, max_messages: int = DEFAULT_MAX_MESSAGES) -> dict[str, Any]:
        subscription_id = str(subscription_id or "").strip()
        if not subscription_id:
            raise HikConnectTeamsError("subscription_id is required.")
        if not 1 <= int(max_messages) <= MAX_MESSAGES:
            raise HikConnectTeamsError(f"max_messages must be between 1 and {MAX_MESSAGES}.")
        return self._authorized_post(
            MESSAGES_PATH, {"subscriptionId": subscription_id, "maxMessages": int(max_messages)}
        )

    def acknowledge(self, batch_id: str) -> dict[str, Any]:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise HikConnectTeamsError("batchId is required.")
        return self._authorized_post(COMPLETE_PATH, {"batchId": batch_id})


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Map documented Teams alarm fields to NexusAI's common event shape."""
    if not isinstance(message, dict):
        raise HikConnectTeamsError("Alarm message must be an object.")
    timestamp = message.get("timestamp")
    try:
        if timestamp is not None:
            stamp = float(timestamp)
            # Event timestamps in this schema are Unix seconds.
            timestamp_iso = datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
        else:
            timestamp_iso = datetime.now(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        raise HikConnectTeamsError("Alarm message contains an invalid timestamp.")
    device_id = str(message.get("deviceId") or "unknown-device")
    channel_id = str(message.get("channelId") or "")
    return {
        "site_id": str(message.get("siteId") or ""),
        "camera_id": f"{device_id}-{channel_id}" if channel_id else device_id,
        "camera_name": str(message.get("channelName") or message.get("deviceName") or device_id),
        "location": str(message.get("deviceName") or "Hik-Connect for Teams"),
        "event": str(message.get("eventTypeName") or message.get("eventType") or "HIKVISION_ALARM"),
        "severity": str(message.get("severity") or "INFO").upper(),
        "timestamp": timestamp_iso,
        "source": "hikconnect-teams",
        "snapshot_available": bool(message.get("picUrl")),
        "snapshot_mime": "image/jpeg",
        "hikconnect_event_id": str(message.get("eventId") or ""),
        "hikconnect_site_id": str(message.get("siteId") or ""),
        "hikconnect_device_id": device_id,
        "hikconnect_channel_id": channel_id or None,
        "hikconnect_event_type": message.get("eventType"),
        "hikconnect_picture_url": str(message.get("picUrl") or ""),
        "hikconnect_data": message,
    }


def process_batch(result: dict[str, Any], event_sink: Callable[[dict[str, Any]], Any],
                  client: HikConnectTeamsClient) -> dict[str, Any]:
    """Persist/process every event via event_sink before acknowledging the batch.

    The sink must be idempotent on hikconnect_event_id. A failed or rejected event
    prevents acknowledgement, allowing safe retry rather than silently losing it.
    """
    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise HikConnectTeamsError("Alarm response data is not an object.")
    batch_id = str(data.get("batchId") or "")
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        raise HikConnectTeamsError("Alarm response messages is not a list.")
    if messages and not batch_id:
        raise HikConnectTeamsError("Non-empty alarm batch has no batchId; refusing to acknowledge.")
    processed = 0
    failures: list[str] = []
    for message in messages:
        try:
            event = normalize_message(message)
            outcome = event_sink(event)
            if outcome is False:
                raise HikConnectTeamsError("Event sink did not confirm durable processing.")
            processed += 1
        except Exception as exc:
            failures.append(str(exc)[:250])
            LOG.exception("Hik-Connect alarm processing failed.")
    if batch_id and not failures:
        client.acknowledge(batch_id)
    return {"batch_id": batch_id, "received": len(messages), "processed": processed,
            "acknowledged": bool(batch_id and not failures), "errors": failures}


@contextmanager
def postgres_consumer_lock(lock_name: str = "nexusai-hikconnect-teams-consumer"):
    """Hold a session-level PostgreSQL advisory lock for one consumer's lifetime.

    Requires DATABASE_URL. Fails closed if PostgreSQL is unavailable; no local-only
    fallback is allowed because it would not protect multiple Render instances.
    """
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise HikConnectTeamsError("DATABASE_URL is required for multi-instance consumer locking.")
    try:
        import psycopg
        # Stable signed 64-bit key derived from a fixed lock name.
        import hashlib
        key = int.from_bytes(hashlib.sha256(lock_name.encode()).digest()[:8], "big", signed=True)
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                acquired = bool(cur.fetchone()[0])
            if not acquired:
                raise HikConnectTeamsError("Another instance already owns the Hik-Connect consumer lock.")
            yield conn
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception:
                pass
            conn.close()
    except HikConnectTeamsError:
        raise
    except Exception as exc:
        raise HikConnectTeamsError("Could not acquire PostgreSQL consumer lock; refusing to poll.") from exc


def run_consumer(client: HikConnectTeamsClient, subscription_id: str,
                 event_sink: Callable[[dict[str, Any]], Any], stop_event: threading.Event) -> None:
    """Run one REST consumer while holding a cross-instance PostgreSQL advisory lock."""
    with postgres_consumer_lock():
        LOG.info("Hik-Connect for Teams consumer acquired exclusive PostgreSQL lock.")
        failures = 0
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                result = client.poll_messages(subscription_id, DEFAULT_MAX_MESSAGES)
                summary = process_batch(result, event_sink, client)
                failures = 0
                if summary["received"]:
                    LOG.info("Hik-Connect batch processed: %s", summary)
            except Exception:
                failures += 1
                delay = min(30.0, max(POLL_INTERVAL_SECONDS, 2 ** min(failures, 5)))
                LOG.exception("Hik-Connect consumer iteration failed; retrying in %.1fs.", delay)
                stop_event.wait(delay)
                continue
            # Keep the documented 500ms cadence without running faster than it.
            stop_event.wait(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - started)))
