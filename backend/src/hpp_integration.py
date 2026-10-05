"""Hik-Partner Pro OpenAPI integration for NexusAI.

This module intentionally keeps Hikvision partner credentials server-side.
Customer CCTV credentials are not accepted here.
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
from typing import Callable, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

LOG = logging.getLogger("nexusai.hpp")

TOKEN_PATH = "/api/hpcgw/v1/token/get"
SITE_SEARCH_PATH = "/api/hpcgw/v1/site/search"
DEVICE_LIST_PATH = "/api/hpcgw/v1/device/list"
CAMERA_LIST_PATH = "/api/hpcgw/v1/device/camera/list"
ALARM_SUBSCRIBE_PATH = "/api/hpcgw/v1/mq/subscribe"
ALARM_MESSAGES_PATH = "/api/hpcgw/v1/mq/messages"
ALARM_PICTURE_URL_PATH = "/api/hpcgw/v1/alarm/pictureurl"

_TOKEN: dict[str, Any] = {}
_TOKEN_LOCK = threading.RLock()


class HppError(RuntimeError):
    pass


class HppSubscribeRequest(BaseModel):
    sub_type: int = Field(default=1, ge=0, le=1)
    sub_mode: str = Field(default="all", pattern="^(all|list)$")
    device_serial_list: list[str] = Field(default_factory=list, max_length=500)


class HppPictureRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=2000)


def _base_url() -> str:
    value = os.getenv("HPP_API_BASE_URL", "").strip().rstrip("/")
    if not value:
        raise HppError("HPP_API_BASE_URL is not configured.")
    return value


def configured() -> bool:
    return all(os.getenv(k, "").strip() for k in ("HPP_API_BASE_URL", "HPP_APP_KEY", "HPP_SECRET_KEY"))


def _request(method: str, path: str, body: dict | None = None, token: str | None = None, timeout: int = 30) -> dict:
    url = _base_url() + path
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
        raise HppError(f"Hik-Partner Pro errorCode={code}: {payload.get('message') or payload.get('errorMsg') or 'request failed'}")
    return payload


def get_token(force: bool = False) -> dict:
    now_ms = int(time.time() * 1000)
    with _TOKEN_LOCK:
        if not force and _TOKEN.get("accessToken") and now_ms < int(_TOKEN.get("expireTime", 0)) - 60_000:
            return dict(_TOKEN)

        payload = _request(
            "POST",
            TOKEN_PATH,
            {"appKey": os.getenv("HPP_APP_KEY", "").strip(), "secretKey": os.getenv("HPP_SECRET_KEY", "").strip()},
            timeout=20,
        )
        data = payload.get("data") or {}
        token = data.get("accessToken")
        if not token:
            raise HppError("Hik-Partner Pro did not return an accessToken.")
        _TOKEN.clear()
        _TOKEN.update(data)
        return dict(_TOKEN)


def call(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    token_data = get_token()
    try:
        return _request("POST", path, body or {}, token=token_data["accessToken"], timeout=timeout)
    except HppError as exc:
        if "LAP500004" not in str(exc):
            raise
        token_data = get_token(force=True)
        return _request("POST", path, body or {}, token=token_data["accessToken"], timeout=timeout)


def search_sites(page: int = 1, page_size: int = 50, search: str = "") -> dict:
    return call(SITE_SEARCH_PATH, {"page": page, "pageSize": page_size, "search": search})


def list_devices(site_id: str = "", page: int = 1, page_size: int = 100, device_serial: str = "") -> dict:
    body: dict[str, Any] = {"page": page, "pageSize": page_size}
    if site_id:
        body["siteId"] = site_id
    if device_serial:
        body["deviceSerial"] = device_serial
    return call(DEVICE_LIST_PATH, body)


def list_cameras(device_serial: str) -> dict:
    return call(CAMERA_LIST_PATH, {"deviceSerial": device_serial})


def subscribe(req: HppSubscribeRequest) -> dict:
    if req.sub_mode == "list" and not req.device_serial_list:
        raise HppError("device_serial_list is required when sub_mode=list.")
    body = {"subType": req.sub_type, "subMode": req.sub_mode}
    if req.sub_mode == "list":
        body["deviceSerialList"] = req.device_serial_list
    return call(ALARM_SUBSCRIBE_PATH, body)


def poll_messages() -> dict:
    # Hik-Partner Pro documents this endpoint as long polling; keep a generous timeout.
    return call(ALARM_MESSAGES_PATH, timeout=35)


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


def _parse_alarm_data(raw: str) -> dict:
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"raw": raw}
    except json.JSONDecodeError:
        parsed = _xml_to_dict(raw)
        return parsed or {"raw": raw[:12000]}


def normalize_message(message: dict) -> dict:
    data = _parse_alarm_data(str(message.get("alarmData", "")))
    channel = data.get("channelID") or data.get("channelId") or data.get("channelNo") or data.get("channel")
    event_type = data.get("eventType") or data.get("eventTypeName") or data.get("event") or data.get("eventTypeCode") or "HIKVISION_ALARM"
    state = data.get("eventState") or data.get("activePostCount") or "active"
    timestamp = data.get("dateTime") or data.get("dateTimeLocal") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "site_id": "",
        "camera_id": f"{message.get('deviceSerial', 'hikvision')}-{channel or 'device'}",
        "camera_name": f"Hikvision {message.get('deviceSerial', '')} Ch {channel}" if channel else f"Hikvision {message.get('deviceSerial', '')}",
        "location": "Hik-Partner Pro",
        "event": str(event_type),
        "severity": "CRITICAL" if str(state).lower() in {"active", "1", "true"} else "INFO",
        "timestamp": str(timestamp),
        "source": "hik-partner-pro",
        "snapshot_available": bool(data.get("picture") or data.get("pictureUrl") or data.get("filePath")),
        "snapshot_mime": "image/jpeg",
        "hpp_device_serial": message.get("deviceSerial"),
        "hpp_channel": channel,
        "hpp_data": data,
        "hpp_batch_id": message.get("batchId"),
        "hpp_format_type": message.get("formatType"),
    }


def _admin_guard(key: str | None) -> None:
    expected = os.getenv("NEXUSAI_ADMIN_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="NexusAI admin key is not configured.")
    if not key or key != expected:
        raise HTTPException(status_code=401, detail="Invalid NexusAI admin key.")


def install(app: FastAPI, event_sink: Callable[[dict], Any] | None = None) -> None:
    @app.get("/api/hpp/status")
    async def hpp_status(x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        return {
            "configured": configured(),
            "provider": "Hik-Partner Pro OpenAPI",
            "security_box_required": False,
            "capabilities": ["site_management", "device_management", "camera_channels", "alarm_subscription", "alarm_long_poll", "alarm_picture_url"],
        }

    @app.post("/api/hpp/test-auth")
    async def hpp_test_auth(x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        if not configured():
            raise HTTPException(status_code=503, detail="Configure HPP_API_BASE_URL, HPP_APP_KEY and HPP_SECRET_KEY first.")
        try:
            token = get_token()
            return {"authenticated": True, "expires_at_ms": token.get("expireTime"), "area_domain": token.get("areaDomain")}
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
            messages = result.get("data") or []
            if isinstance(messages, dict):
                messages = messages.get("messages") or messages.get("alarmMessages") or []
            normalized = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                event = normalize_message(message)
                normalized.append(event)
                if event_sink:
                    try:
                        event_sink(event)
                    except Exception:
                        LOG.exception("Failed to persist HPP event.")
            return {"received": len(normalized), "events": normalized, "raw": result}
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/hpp/alarms/picture-url")
    async def hpp_alarm_picture_url(payload: HppPictureRequest, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        try:
            return picture_url(payload.file_path)
        except HppError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
