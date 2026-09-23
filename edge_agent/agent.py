import json
import logging
import os
import socket
import time
import threading
import re
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("NEXUSAI_API_URL", "https://nexusai-aphile.workers.dev").rstrip("/")
EDGE_AGENT_TOKEN = os.getenv("NEXUSAI_EDGE_TOKEN", "")
SITE_ID = os.getenv("NEXUSAI_SITE_ID", "site-unknown")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
RECONNECT_SECONDS = int(os.getenv("RECONNECT_SECONDS", "10"))
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
LOG_FILE = os.getenv("LOG_FILE", "nexusai_edge.log")
LOCAL_AGENT_HOST = os.getenv("LOCAL_AGENT_HOST", "127.0.0.1")
LOCAL_AGENT_PORT = int(os.getenv("LOCAL_AGENT_PORT", "8787"))

ACTIVE_SESSIONS = {}
ACTIVE_SESSIONS_LOCK = threading.Lock()

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

EVENT_NAMES = {
    "weapon": ("WEAPON DETECTED", "CRITICAL"),
    "gun": ("WEAPON DETECTED", "CRITICAL"),
    "firearm": ("WEAPON DETECTED", "CRITICAL"),
    "knife": ("WEAPON DETECTED", "CRITICAL"),
    "threat": ("THREAT DETECTED", "CRITICAL"),
    "theft": ("THEFT DETECTED", "CRITICAL"),
    "stealing": ("THEFT DETECTED", "CRITICAL"),
    "shoplifting": ("THEFT DETECTED", "CRITICAL"),
    "objectremoval": ("OBJECT REMOVAL DETECTED", "HIGH"),
    "intrusion": ("INTRUSION DETECTED", "HIGH"),
    "linedetection": ("LINE CROSSING DETECTED", "HIGH"),
    "linecrossing": ("LINE CROSSING DETECTED", "HIGH"),
    "regionentrance": ("AREA ENTRY DETECTED", "HIGH"),
    "regionexiting": ("AREA EXIT DETECTED", "MEDIUM"),
    "loitering": ("LOITERING DETECTED", "MEDIUM"),
    "motion": ("MOTION DETECTED", "LOW"),
}

def camera_config():
    return {
        "camera_id": os.getenv("CAMERA_ID", "camera-1"),
        "camera_name": os.getenv("CAMERA_NAME", "Camera 1"),
        "camera_ip": os.getenv("CAM_IP", ""),
        "camera_port": int(os.getenv("CAM_PORT", "80")),
        "username": os.getenv("CAM_USER", ""),
        "password": os.getenv("CAM_PASS", ""),
        "location": os.getenv("LOCATION", "Main Entrance"),
        "snapshot_channel": os.getenv("SNAPSHOT_CHANNEL", "101"),
    }

def auth(cfg):
    return HTTPDigestAuth(cfg["username"], cfg["password"])

def headers():
    return {
        "Authorization": f"Bearer {EDGE_AGENT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "NexusAI-Edge-Agent/1.0",
    }

def post_backend(path, payload):
    if not EDGE_AGENT_TOKEN:
        logging.error("NEXUSAI_EDGE_TOKEN is missing")
        return False
    try:
        response = requests.post(
            f"{API_BASE_URL}{path}",
            json=payload,
            headers=headers(),
            timeout=10,
        )
        if 200 <= response.status_code < 300:
            return True
        logging.warning("Backend %s returned HTTP %s: %s",
                        path, response.status_code, response.text[:300])
    except requests.RequestException as exc:
        logging.warning("Backend connection failed: %s", exc)
    return False

def device_info(cfg):
    url = f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/System/deviceInfo"
    response = requests.get(url, auth=auth(cfg), timeout=8)
    response.raise_for_status()
    return response.text


def discover_channels(cfg):
    """Discover usable Hikvision NVR/DVR channels via ISAPI."""
    urls = [
        f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/Streaming/channels",
        f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/ContentMgmt/StreamingProxy/channels",
    ]
    last_error = None
    for url in urls:
        try:
            response = requests.get(url, auth=auth(cfg), timeout=10)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            root = ET.fromstring(response.text)
            channels = []
            for node in root.iter():
                if node.tag.split("}")[-1] != "StreamingChannel":
                    continue
                values = {}
                for child in node.iter():
                    key = child.tag.split("}")[-1]
                    if child is not node and child.text:
                        values.setdefault(key, child.text.strip())
                channel_id = values.get("id")
                if not channel_id:
                    continue
                enabled = values.get("enabled", "true").lower() != "false"
                channels.append({
                    "channel_id": str(channel_id),
                    "channel_name": values.get("channelName") or str(channel_id),
                    "enabled": enabled,
                    "video_input_channel_id": values.get("dynVideoInputChannelID") or values.get("videoInputChannelID"),
                })
            unique = {item["channel_id"]: item for item in channels if item["enabled"]}
            if unique:
                return list(unique.values())
        except (requests.RequestException, ET.ParseError) as exc:
            last_error = str(exc)
    if last_error:
        logging.info("Channel discovery unavailable: %s", last_error)
    return []


def verify_camera(cfg):
    result = {
        "camera_id": cfg["camera_id"],
        "camera_name": cfg["camera_name"],
        "location": cfg["location"],
        "network": "FAILED",
        "camera": "FAILED",
        "credentials": "NOT_CHECKED",
        "nexusai": "EDGE_AGENT_READY",
        "verified": False,
        "device_type": "UNKNOWN",
        "channels": [],
    }
    try:
        with socket.create_connection((cfg["camera_ip"], cfg["camera_port"]), timeout=5):
            result["network"] = "CONNECTED"
    except OSError as exc:
        result["error"] = f"Camera/NVR unreachable: {exc}"
        return result

    try:
        xml = device_info(cfg)
        result["camera"] = "DETECTED"
        result["credentials"] = "ACCEPTED"
        try:
            root = ET.fromstring(xml)
            values = {}
            for child in root.iter():
                key = child.tag.split("}")[-1]
                if child.text:
                    values.setdefault(key, child.text.strip())
            device_type = (values.get("deviceType") or "").strip()
            result["device_type"] = device_type or "HIKVISION_DEVICE"
        except ET.ParseError:
            result["device_type"] = "HIKVISION_DEVICE"

        channels = discover_channels(cfg)
        if channels:
            result["channels"] = channels
            result["device_type"] = "NVR" if "NVR" in result["device_type"].upper() or len(channels) > 1 else result["device_type"]
        result["verified"] = True
        result["nexusai"] = "CONNECTED"
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            result["camera"] = "DETECTED"
            result["credentials"] = "REJECTED"
            result["error"] = "Incorrect camera/NVR username or password"
        else:
            result["error"] = f"Hikvision returned HTTP {exc.response.status_code if exc.response else 'ERROR'}"
    except requests.RequestException as exc:
        result["error"] = f"Camera/NVR request failed: {exc}"
    return result

def capture_snapshot(cfg):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    url = (
        f"http://{cfg['camera_ip']}:{cfg['camera_port']}"
        f"/ISAPI/Streaming/channels/{cfg['snapshot_channel']}/picture"
    )
    try:
        response = requests.get(url, auth=auth(cfg), timeout=8)
        if response.status_code != 200:
            logging.warning("Snapshot failed: HTTP %s", response.status_code)
            return None
        filename = SNAPSHOT_DIR / (
            f"{cfg['camera_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        )
        filename.write_bytes(response.content)
        return str(filename)
    except requests.RequestException as exc:
        logging.warning("Snapshot request failed: %s", exc)
        return None

def classify_event(payload):
    lower = payload.lower()
    for keyword, definition in EVENT_NAMES.items():
        if keyword in lower:
            return definition
    return ("SECURITY EVENT DETECTED", "MEDIUM")

def extract_channel_id(raw_event):
    patterns = [
        r"<channelID>\\s*([^<]+)\\s*</channelID>",
        r"<dynChannelID>\\s*([^<]+)\\s*</dynChannelID>",
        r'"channelID"\\s*:\\s*"?(\\d+)',
        r'"dynChannelID"\\s*:\\s*"?(\\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_event, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def send_event(cfg, raw_event, channel=None):
    event_name, severity = classify_event(raw_event)
    timestamp = datetime.now(timezone.utc).isoformat()
    event_cfg = dict(cfg)
    if channel:
        event_cfg["camera_id"] = f"{cfg['camera_id']}-ch-{channel['channel_id']}"
        event_cfg["camera_name"] = channel.get("channel_name") or f"{cfg['camera_name']} CH {channel['channel_id']}"
        event_cfg["snapshot_channel"] = channel["channel_id"]
    snapshot = capture_snapshot(event_cfg)
    payload = {
        "site_id": SITE_ID,
        "camera_id": event_cfg["camera_id"],
        "camera_name": event_cfg["camera_name"],
        "location": event_cfg["location"],
        "event": event_name,
        "severity": severity,
        "timestamp": timestamp,
        "source": "nexusai-edge-agent",
        "snapshot_available": bool(snapshot),
    }
    delivered = post_backend("/api/edge/events", payload)
    logging.info("Event=%s severity=%s camera=%s delivered=%s",
                 event_name, severity, event_cfg["camera_name"], delivered)

def heartbeat(cfg, verification=None):
    payload = {
        "site_id": SITE_ID,
        "agent_version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ONLINE",
        "cameras": [{
            "camera_id": cfg["camera_id"],
            "camera_name": cfg["camera_name"],
            "location": cfg["location"],
            "verified": bool(verification and verification.get("verified")),
        }],
    }
    post_backend("/api/edge/heartbeat", payload)

def monitor_device(cfg, channels=None):
    """Start a background Hikvision event listener for a camera or NVR."""
    session_key = f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}"
    with ACTIVE_SESSIONS_LOCK:
        if session_key in ACTIVE_SESSIONS:
            return False, "ALREADY_ACTIVE"
        ACTIVE_SESSIONS[session_key] = True

    channel_map = {str(c["channel_id"]): c for c in (channels or [])}

    def worker():
        url = (
            f"http://{cfg['camera_ip']}:{cfg['camera_port']}"
            "/ISAPI/Event/notification/alertStream"
        )
        try:
            while True:
                try:
                    logging.info("Connecting to Hikvision event stream: %s", url)
                    with requests.get(url, auth=auth(cfg), stream=True, timeout=60) as response:
                        if response.status_code == 401:
                            logging.error("Camera/NVR authentication failed")
                            time.sleep(RECONNECT_SECONDS)
                            continue
                        if response.status_code != 200:
                            logging.warning("Event stream returned HTTP %s", response.status_code)
                            time.sleep(RECONNECT_SECONDS)
                            continue
                        logging.info("Hikvision event stream connected")
                        for line in response.iter_lines():
                            if not line:
                                continue
                            decoded = line.decode("utf-8", errors="ignore")
                            if not any(keyword in decoded.lower() for keyword in EVENT_NAMES):
                                continue
                            channel_id = extract_channel_id(decoded)
                            send_event(cfg, decoded, channel_map.get(channel_id))
                except requests.RequestException as exc:
                    logging.warning("Event stream disconnected: %s", exc)
                except Exception:
                    logging.exception("Unexpected edge-agent monitoring error")
                time.sleep(RECONNECT_SECONDS)
        finally:
            with ACTIVE_SESSIONS_LOCK:
                ACTIVE_SESSIONS.pop(session_key, None)

    threading.Thread(target=worker, daemon=True, name=f"nexusai-monitor-{cfg['camera_ip']}").start()
    return True, "MONITORING_STARTED"

class LocalAgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json(204, {})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "service": "NexusAI Edge Agent",
                "status": "ONLINE",
                "version": "1.1.0",
            })
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path not in ("/verify", "/activate"):
            self._send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            required = ["camera_name", "camera_ip", "camera_port", "username", "password", "location"]
            if any(not payload.get(key) for key in required):
                self._send_json(400, {"error": "All camera/NVR details are required"})
                return

            cfg = {
                "camera_id": payload.get("camera_id", f"camera-{int(time.time())}"),
                "camera_name": str(payload["camera_name"]),
                "camera_ip": str(payload["camera_ip"]),
                "camera_port": int(payload["camera_port"]),
                "username": str(payload["username"]),
                "password": str(payload["password"]),
                "location": str(payload["location"]),
                "snapshot_channel": str(payload.get("snapshot_channel", "101")),
            }
            result = verify_camera(cfg)
            if result.get("verified"):
                if self.path == "/activate":
                    started, monitor_status = monitor_device(cfg, result.get("channels"))
                    result["monitoring"] = monitor_status
                    result["monitoring_started"] = started
                post_backend("/api/edge/verify", {"site_id": SITE_ID, **result})
            self._send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"Invalid request: {exc}"})
        except Exception as exc:
            logging.exception("Local verification API error")
            self._send_json(500, {"error": "Edge Agent verification failed"})

    def log_message(self, format, *args):
        logging.info("Local API: " + format, *args)


def start_local_api():
    server = ThreadingHTTPServer((LOCAL_AGENT_HOST, LOCAL_AGENT_PORT), LocalAgentHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="nexusai-local-api").start()
    logging.info("Local verification API listening on %s:%s", LOCAL_AGENT_HOST, LOCAL_AGENT_PORT)
    print(f"Local verification API: http://{LOCAL_AGENT_HOST}:{LOCAL_AGENT_PORT}")
    return server


def heartbeat_loop(cfg, verification):
    while True:
        try:
            heartbeat(cfg, verification)
        except Exception:
            logging.exception("Heartbeat loop error")
        time.sleep(HEARTBEAT_SECONDS)


def main():
    print("=" * 60)
    print("NEXUSAI EDGE AGENT")
    print("=" * 60)
    print(f"Site:     {SITE_ID}")
    print(f"Camera:   {os.getenv('CAMERA_NAME', 'Camera 1')}")
    print(f"Location: {os.getenv('LOCATION', 'Main Entrance')}")
    print(f"Backend:  {API_BASE_URL}")
    print("=" * 60)

    start_local_api()

    cfg = camera_config()
    if not all([cfg["camera_ip"], cfg["username"], cfg["password"]]):
        raise SystemExit("Missing CAM_IP, CAM_USER, or CAM_PASS in .env")

    verification = verify_camera(cfg)
    print(json.dumps(verification, indent=2))
    if not verification.get("verified"):
        logging.error("Camera verification failed: %s", verification)
        raise SystemExit(1)

    post_backend("/api/edge/verify", {"site_id": SITE_ID, **verification})
    heartbeat(cfg, verification)
    threading.Thread(
        target=heartbeat_loop,
        args=(cfg, verification),
        daemon=True,
        name="nexusai-heartbeat",
    ).start()
    channels = verification.get("channels", [])
    monitor_device(cfg, channels)
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
