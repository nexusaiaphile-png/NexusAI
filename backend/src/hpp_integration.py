"""NexusAI Hik-Partner Pro OpenAPI integration.

Built against the uploaded Hik-Partner Pro OpenAPI V2.15.500 Developer Guide.
HPP credentials remain server-side. Customer CCTV credentials are never accepted.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

LOG = logging.getLogger("nexusai.hpp")

TOKEN_URL = "https://api.hik-partner.com"
TOKEN_PATH = "/api/hpcgw/v1/token/get"
SITE_SEARCH_PATH = "/api/hpcgw/v1/site/search"
DEVICE_LIST_PATH = "/api/hpcgw/v1/device/list"
CAMERA_LIST_PATH = "/api/hpcgw/v1/device/camera/list"
ALARM_SUBSCRIBE_PATH = "/api/hpcgw/v1/mq/subscribe"
ALARM_MESSAGES_PATH = "/api/hpcgw/v1/mq/messages"
ALARM_OFFSET_PATH = "/api/hpcgw/v1/mq/offset"
ALARM_PICTURE_URL_PATH = "/api/hpcgw/v1/alarm/pictureurl"

_TOKEN: dict[str, Any] = {}
_TOKEN_LOCK = threading.RLock()
_WORKER: threading.Thread | None = None
_STOP = threading.Event()


class HppError(RuntimeError):
    pass


class HppSubscribeRequest(BaseModel):
    sub_type: int = Field(default=1, ge=0, le=1)
    sub_mode: str = Field(default="all", pattern="^(all|list)$")
    device_serial_list: list[str] = Field(default_factory=list, max_length=500)


class HppPictureRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=2000)


def configured() -> bool:
    return all(os.getenv(k, "").strip() for k in ("HPP_APP_KEY", "HPP_SECRET_KEY"))


def _service_base() -> str:
    with _TOKEN_LOCK:
        domain = str(_TOKEN.get("areaDomain") or "").strip().rstrip("/")
    if not domain:
        raise HppError("HPP regional areaDomain is not available. Authenticate first.")
    return domain


def _request(base: str, method: str, path: str, body: dict | None = None,
             token: str | None = None, timeout: int = 30) -> dict:
    url = base.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HppError(f"Hik-Partner Pro HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise HppError(f"Hik-Partner Pro connection failed: {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HppError("Hik-Partner Pro returned a non-JSON response.") from exc

    code = str(payload.get("errorCode", "0"))
    if code != "0":
        raise HppError(
            f"Hik-Partner Pro errorCode={code}: "
            f"{payload.get('message') or payload.get('errorMsg') or 'request failed'}"
        )
    return payload


def get_token(force: bool = False) -> dict:
    now_ms = int(time.time() * 1000)
    with _TOKEN_LOCK:
        if (
            not force
            and _TOKEN.get("accessToken")
            and now_ms < int(_TOKEN.get("expireTime", 0)) - 60_000
            and _TOKEN.get("areaDomain")
        ):
            return dict(_TOKEN)

        payload = _request(
            TOKEN_URL,
            "POST",
            TOKEN_PATH,
            {"appKey": os.getenv("HPP_APP_KEY", "").strip(),
             "secretKey": os.getenv("HPP_SECRET_KEY", "").strip()},
            timeout=20,
        )
        data = payload.get("data") or {}
        if not data.get("accessToken") or not data.get("areaDomain"):
            raise HppError("Hik-Partner Pro did not return accessToken and areaDomain.")
        _TOKEN.clear()
        _TOKEN.update(data)
        return dict(_TOKEN)


def call(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    token_data = get_token()
    try:
        return _request(
            str(token_data["areaDomain"]), "POST", path, body or {},
            token=token_data["accessToken"], timeout=timeout
        )
    except HppError as exc:
        if "LAP500004" not in str(exc):
            raise
        token_data = get_token(force=True)
        return _request(
            str(token_data["areaDomain"]), "POST", path, body or {},
            token=token_data["accessToken"], timeout=timeout
        )


def search_sites(page: int = 1, page_size: int = 50, search: str = "") -> dict:
    return call(SITE_SEARCH_PATH, {"page": page, "pageSize": page_size, "search": search})


def list_devices(site_id: str = "", page: int = 1, page_size: int = 100,
                 device_serial: str = "") -> dict:
    body: dict[str, Any] = {"page": page, "pageSize": page_size}
    if site_id:
        body["siteId"] = site_id
    if device_serial:
        body["deviceSerial"] = device_serial
    return call(DEVICE_LIST_PATH, body)


def list_cameras(device_serial: str) -> dict:
    if not device_serial:
        raise HppError("deviceSerial is required.")
    return call(CAMERA_LIST_PATH, {"deviceSerial": device_serial})


def subscribe(req: HppSubscribeRequest) -> dict:
    if req.sub_mode == "list" and not req.device_serial_list:
        raise HppError("deviceSerialList is required when subMode=list.")
    body: dict[str, Any] = {"subType": req.sub_type, "subMode": req.sub_mode}
    if req.sub_mode == "list":
        body["deviceSerialList"] = req.device_serial_list
    return call(ALARM_SUBSCRIBE_PATH, body)


def poll_messages() -> dict:
    # The HPP guide documents this as long polling; no event returns after ~20s.
    return call(ALARM_MESSAGES_PATH, timeout=35)


def acknowledge(batch_id: str) -> dict:
    if not batch_id:
        raise HppError("batchId is required.")
    return call(ALARM_OFFSET_PATH, {"batchId": batch_id})


def picture_url(file_path: str) -> dict:
    return call(ALARM_PICTURE_URL_PATH, {"filePath": file_path})


def _xml_to_dict(raw: str) -> dict:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    result: dict[str, Any] = {}
    for child in root.iter():
        if child is root:
            continue
        key = child.tag.rsplit("}", 1)[-1]
        if child.text and child.text.strip() and key not in result:
            result[key] = child.text.strip()
    return result


def _parse_alarm_data(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"raw": raw}
    except json.JSONDecodeError:
        parsed = _xml_to_dict(raw)
        return parsed or {"raw": raw[:12000]}


def _first(data: dict, *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_message(message: dict, hpp_site_id: str = "") -> dict:
    data = _parse_alarm_data(message.get("alarmData"))
    nested = data.get("EventNotificationAlert") if isinstance(data.get("EventNotificationAlert"), dict) else data
    channel = _first(nested, "channelID", "channelId", "channelNo", "channel")
    event_type = _first(
        nested, "eventType", "eventTypeName", "event", "eventTypeCode",
        "description", "eventDescription"
    ) or "HIKVISION_ALARM"
    timestamp = _first(nested, "triggerTime", "dateTime", "dateTimeLocal", "timestamp")
    timestamp = str(timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    state = str(_first(nested, "eventState", "activePostCount", "state") or "active").lower()

    picture = _first(nested, "pictureUrl", "picture", "filePath", "url")
    severity = "CRITICAL" if state in {"active", "1", "true", "alarm"} else "INFO"

    serial = str(message.get("deviceSerial") or nested.get("deviceSerial") or "")
    camera_id = f"{serial}-{channel}" if channel else serial or "hikvision-device"
    return {
        "site_id": hpp_site_id,
        "camera_id": camera_id,
        "camera_name": f"Hikvision {serial} Ch {channel}" if channel else f"Hikvision {serial}",
        "location": "Hik-Partner Pro",
        "event": str(event_type),
        "severity": severity,
        "timestamp": timestamp,
        "source": "hik-partner-pro",
        "snapshot_available": bool(picture),
        "snapshot_mime": "image/jpeg",
        "hpp_site_id": hpp_site_id,
        "hpp_device_serial": serial,
        "hpp_channel": str(channel) if channel is not None else None,
        "hpp_data": data,
    }


def _admin_guard(key: str | None) -> None:
    expected = os.getenv("NEXUSAI_ADMIN_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="NexusAI admin key is not configured.")
    if not key or key != expected:
        raise HTTPException(status_code=401, detail="Invalid NexusAI admin key.")


def _process_batch(result: dict, event_sink: Callable[[dict], Any] | None) -> dict:
    data = result.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    batch_id = str(data.get("batchId") or "")
    messages = data.get("list") or []
    if not isinstance(messages, list):
        messages = []

    accepted = 0
    skipped = 0
    errors = []
    for message in messages:
        if not isinstance(message, dict):
            skipped += 1
            continue
        try:
            # event_sink is responsible for resolving the HPP site/device to a NexusAI site.
            event = normalize_message(message)
            if event_sink:
                mapped = event_sink(event)
                if mapped:
                    accepted += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(str(exc)[:300])
            LOG.exception("Failed to process HPP alarm message.")

    # Only acknowledge the HPP batch after processing all messages. Otherwise HPP may
    # re-send the batch, which is preferable to silently losing an alarm.
    if batch_id and not errors:
        acknowledge(batch_id)

    return {
        "batch_id": batch_id,
        "received": len(messages),
        "accepted": accepted,
        "skipped": skipped,
        "errors": errors,
    }


def _worker_loop(event_sink: Callable[[dict], Any] | None) -> None:
    LOG.info("NexusAI HPP alarm worker started.")
    failures = 0
    while not _STOP.is_set():
        try:
            result = poll_messages()
            summary = _process_batch(result, event_sink)
            failures = 0
            if summary["received"]:
                LOG.info("HPP batch %s: %s", summary["batch_id"], summary)
        except Exception:
            failures += 1
            delay = min(60, 2 ** min(failures, 6))
            LOG.exception("HPP alarm worker error; retrying in %ss.", delay)
            _STOP.wait(delay)


def start_worker(event_sink: Callable[[dict], Any] | None) -> bool:
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return False
    if not configured():
        return False
    _STOP.clear()
    _WORKER = threading.Thread(
        target=_worker_loop, args=(event_sink,),
        name="nexusai-hpp-alarms", daemon=True
    )
    _WORKER.start()
    return True


def stop_worker() -> None:
    _STOP.set()


def install(app: FastAPI, event_sink: Callable[[dict], Any] | None = None) -> None:
    @app.get("/api/hpp/status")
    async def hpp_status(x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        return {
            "configured": configured(),
            "provider": "Hik-Partner Pro OpenAPI V2.15.500",
            "alarm_worker": bool(_WORKER and _WORKER.is_alive()),
            "security_box_required": False,
            "capabilities": [
                "site_management", "device_management", "camera_channels",
                "alarm_subscription", "alarm_long_poll", "alarm_offset",
                "alarm_picture_url"
            ],
        }

    @app.post("/api/hpp/test-auth")
    async def hpp_test_auth(x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        if not configured():
            raise HTTPException(status_code=503, detail="Set HPP_APP_KEY and HPP_SECRET_KEY on the server first.")
        try:
            token = get_token()
            return {
                "authenticated": True,
                "expires_at_ms": token.get("expireTime"),
                "area_domain": token.get("areaDomain"),
            }
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/sites/search")
    async def hpp_sites_search(payload: dict | None = None, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        payload = payload or {}
        try:
            return search_sites(int(payload.get("page", 1)), int(payload.get("pageSize", 50)), str(payload.get("search", "")))
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/devices/list")
    async def hpp_devices_list(payload: dict | None = None, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        payload = payload or {}
        try:
            return list_devices(str(payload.get("siteId", "")), int(payload.get("page", 1)), int(payload.get("pageSize", 100)), str(payload.get("deviceSerial", "")))
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/cameras/list")
    async def hpp_cameras_list(payload: dict, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            return list_cameras(str(payload.get("deviceSerial", "")))
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/alarms/subscribe")
    async def hpp_alarm_subscribe(payload: HppSubscribeRequest, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            return subscribe(payload)
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/alarms/poll")
    async def hpp_alarm_poll(x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            result = poll_messages()
            return _process_batch(result, event_sink)
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/alarms/offset")
    async def hpp_alarm_offset(payload: dict, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            return acknowledge(str(payload.get("batchId", "")))
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/alarm/picture-url")
    async def hpp_alarm_picture_url(payload: HppPictureRequest, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            return picture_url(payload.file_path)
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.on_event("startup")
    async def hpp_startup():
        enabled = os.getenv("HPP_ALARM_WORKER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        if enabled and configured():
            start_worker(event_sink)

