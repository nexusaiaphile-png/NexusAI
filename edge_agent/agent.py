import json
import logging
import os
import socket
import time
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
    }
    try:
        with socket.create_connection((cfg["camera_ip"], cfg["camera_port"]), timeout=5):
            result["network"] = "CONNECTED"
    except OSError as exc:
        result["error"] = f"Camera unreachable: {exc}"
        return result

    url = f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/System/deviceInfo"
    try:
        response = requests.get(url, auth=auth(cfg), timeout=8)
        if response.status_code == 200:
            result["camera"] = "DETECTED"
            result["credentials"] = "ACCEPTED"
            result["verified"] = True
        elif response.status_code == 401:
            result["camera"] = "DETECTED"
            result["credentials"] = "REJECTED"
            result["error"] = "Incorrect camera username or password"
        else:
            result["camera"] = f"HTTP_{response.status_code}"
            result["error"] = f"Hikvision returned HTTP {response.status_code}"
    except requests.RequestException as exc:
        result["error"] = f"Camera request failed: {exc}"
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

def send_event(cfg, raw_event):
    event_name, severity = classify_event(raw_event)
    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot = capture_snapshot(cfg)
    payload = {
        "site_id": SITE_ID,
        "camera_id": cfg["camera_id"],
        "camera_name": cfg["camera_name"],
        "location": cfg["location"],
        "event": event_name,
        "severity": severity,
        "timestamp": timestamp,
        "source": "nexusai-edge-agent",
        "snapshot_available": bool(snapshot),
    }
    delivered = post_backend("/api/edge/events", payload)
    logging.info("Event=%s severity=%s camera=%s delivered=%s",
                 event_name, severity, cfg["camera_name"], delivered)

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

def listen(cfg):
    url = (
        f"http://{cfg['camera_ip']}:{cfg['camera_port']}"
        "/ISAPI/Event/notification/alertStream"
    )
    while True:
        try:
            logging.info("Connecting to Hikvision event stream: %s", url)
            with requests.get(url, auth=auth(cfg), stream=True, timeout=60) as response:
                if response.status_code == 401:
                    logging.error("Camera authentication failed")
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
                    if any(keyword in decoded.lower() for keyword in EVENT_NAMES):
                        send_event(cfg, decoded)
        except requests.RequestException as exc:
            logging.warning("Event stream disconnected: %s", exc)
        except Exception:
            logging.exception("Unexpected edge-agent error")
        time.sleep(RECONNECT_SECONDS)

def main():
    print("=" * 60)
    print("NEXUSAI EDGE AGENT")
    print("=" * 60)
    print(f"Site:     {SITE_ID}")
    print(f"Camera:   {os.getenv('CAMERA_NAME', 'Camera 1')}")
    print(f"Location: {os.getenv('LOCATION', 'Main Entrance')}")
    print(f"Backend:  {API_BASE_URL}")
    print("=" * 60)

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
    listen(cfg)

if __name__ == "__main__":
    main()
