import json
import logging
import os
import socket
import time
import ipaddress
import concurrent.futures

import threading
import re
import secrets
import ssl
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

import requests
import qrcode
import io
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("NEXUSAI_API_URL", "https://nexusai-worker.onrender.com").rstrip("/")
EDGE_AGENT_TOKEN = os.getenv("NEXUSAI_EDGE_TOKEN", "")
SITE_ID = os.getenv("NEXUSAI_SITE_ID", "site-unknown")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
RECONNECT_SECONDS = int(os.getenv("RECONNECT_SECONDS", "10"))
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
LOG_FILE = os.getenv("LOG_FILE", "nexusai_edge.log")
LOCAL_AGENT_HOST = os.getenv("LOCAL_AGENT_HOST", "0.0.0.0")
LOCAL_AGENT_PORT = int(os.getenv("LOCAL_AGENT_PORT", "8787"))
SITE_CONFIG_PATH = Path(os.getenv("SITE_CONFIG_PATH", str(Path.home() / ".nexusai_site.json")))

# Load a locally persisted site pairing so an Edge Agent restart does not reset the site.
try:
    if SITE_CONFIG_PATH.exists():
        saved_site = json.loads(SITE_CONFIG_PATH.read_text(encoding="utf-8"))
        persisted_site_id = str(saved_site.get("site_id", "")).strip()
        if persisted_site_id:
            SITE_ID = persisted_site_id
except (OSError, json.JSONDecodeError):
    logging.warning("Could not load persisted NexusAI site configuration")

ACTIVE_SESSIONS = {}
ACTIVE_SESSIONS_LOCK = threading.Lock()
HEARTBEAT_THREADS = {}
PAIR_TOKENS = {}
PAIR_SESSIONS = {}
PAIR_LOCK = threading.Lock()
PAIR_TTL_SECONDS = int(os.getenv("PAIR_TTL_SECONDS", "300"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
LOCAL_AGENT_TLS_CERT = os.getenv("LOCAL_AGENT_TLS_CERT", "").strip()
LOCAL_AGENT_TLS_KEY = os.getenv("LOCAL_AGENT_TLS_KEY", "").strip()

def _now():
    return time.time()

def _new_token():
    return secrets.token_urlsafe(32)

def _cleanup_pairing():
    now = _now()
    with PAIR_LOCK:
        for store in (PAIR_TOKENS, PAIR_SESSIONS):
            expired = [key for key, item in store.items() if float(item.get("expires_at", 0)) <= now]
            for key in expired:
                store.pop(key, None)

def create_pair_token():
    _cleanup_pairing()
    token = _new_token()
    with PAIR_LOCK:
        PAIR_TOKENS[token] = {"site_id": SITE_ID, "expires_at": _now() + PAIR_TTL_SECONDS}
    return token

def valid_pair_token(token):
    _cleanup_pairing()
    with PAIR_LOCK:
        item = PAIR_TOKENS.get(token or "")
        return bool(item and item.get("site_id") == SITE_ID and float(item.get("expires_at", 0)) > _now())

def claim_pair_token(token):
    _cleanup_pairing()
    with PAIR_LOCK:
        item = PAIR_TOKENS.pop(token, None)
        if not item or item.get("site_id") != SITE_ID:
            return None
        session = _new_token()
        PAIR_SESSIONS[session] = {"site_id": SITE_ID, "expires_at": _now() + SESSION_TTL_SECONDS}
        return session

def valid_session(token):
    _cleanup_pairing()
    with PAIR_LOCK:
        item = PAIR_SESSIONS.get(token or "")
        return bool(item and item.get("site_id") == SITE_ID and float(item.get("expires_at", 0)) > _now())

def local_access_allowed(handler):
    return handler.client_address[0] in {"127.0.0.1", "::1"}

def require_mobile_session(handler):
    auth_header = handler.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not valid_session(token):
        handler._send_json(401, {"error": "Pair this phone with the NexusAI site first."})
        return False
    return True

def local_access_urls():
    try:
        ip = str(local_network().network_address)
        # Use the actual interface address rather than the subnet address.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        ip = "127.0.0.1"
    scheme = "https" if LOCAL_AGENT_TLS_CERT and LOCAL_AGENT_TLS_KEY else "http"
    return [f"{scheme}://{ip}:{LOCAL_AGENT_PORT}"]

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

EVENT_NAMES = {
    "weapon": ("WEAPON DETECTED", "CRITICAL"), "gun": ("WEAPON DETECTED", "CRITICAL"),
    "firearm": ("WEAPON DETECTED", "CRITICAL"), "knife": ("WEAPON DETECTED", "CRITICAL"),
    "threat": ("THREAT DETECTED", "CRITICAL"), "theft": ("THEFT DETECTED", "CRITICAL"),
    "stealing": ("THEFT DETECTED", "CRITICAL"), "shoplifting": ("THEFT DETECTED", "CRITICAL"),
    "objectremoval": ("OBJECT REMOVAL DETECTED", "HIGH"), "intrusion": ("INTRUSION DETECTED", "HIGH"),
    "linedetection": ("LINE CROSSING DETECTED", "HIGH"), "linecrossing": ("LINE CROSSING DETECTED", "HIGH"),
    "regionentrance": ("AREA ENTRY DETECTED", "HIGH"), "regionexiting": ("AREA EXIT DETECTED", "MEDIUM"),
    "loitering": ("LOITERING DETECTED", "MEDIUM"), "motion": ("MOTION DETECTED", "LOW"),
}

def camera_config():
    return {"camera_id": os.getenv("CAMERA_ID", "camera-1"), "camera_name": os.getenv("CAMERA_NAME", "Camera 1"),
            "camera_ip": os.getenv("CAM_IP", ""), "camera_port": int(os.getenv("CAM_PORT", "80")),
            "username": os.getenv("CAM_USER", ""), "password": os.getenv("CAM_PASS", ""),
            "location": os.getenv("LOCATION", "Main Entrance"), "snapshot_channel": os.getenv("SNAPSHOT_CHANNEL", "101")}

def auth(cfg): return HTTPDigestAuth(cfg["username"], cfg["password"])

def headers():
    return {"Authorization": f"Bearer {EDGE_AGENT_TOKEN}", "Content-Type": "application/json",
            "User-Agent": "NexusAI-Edge-Agent/1.0"}

def post_backend(path, payload):
    if not EDGE_AGENT_TOKEN:
        logging.error("NEXUSAI_EDGE_TOKEN is missing")
        return False
    try:
        response = requests.post(f"{API_BASE_URL}{path}", json=payload, headers=headers(), timeout=10)
        if 200 <= response.status_code < 300: return True
        logging.warning("Backend %s returned HTTP %s: %s", path, response.status_code, response.text[:300])
    except requests.RequestException as exc: logging.warning("Backend connection failed: %s", exc)
    return False

def device_info(cfg):
    response = requests.get(f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/System/deviceInfo",
                            auth=auth(cfg), timeout=8)
    response.raise_for_status()
    return response.text

def local_network():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80)); local_ip = sock.getsockname()[0]
    finally: sock.close()
    return ipaddress.ip_network(f"{local_ip}/24", strict=False)

def probe_hikvision(ip):
    for port in (80, 443, 8000):
        try:
            with socket.create_connection((ip, port), timeout=0.35):
                scheme = "https" if port == 443 else "http"
                response = requests.get(f"{scheme}://{ip}/ISAPI/System/deviceInfo", timeout=0.8, allow_redirects=False, verify=False)
                server = (response.headers.get("Server") or "").lower(); body = response.text[:2000].lower()
                if response.status_code == 401 or "hikvision" in server or "hikvision" in body:
                    return {"name":"Hikvision device","ip":ip,"port":443 if scheme=="https" else 80,"type":"Hikvision","discovery":"LOCAL_NETWORK"}
        except (OSError, requests.RequestException):
            continue
    return None

def discover_local_devices():
    try:
        hosts=[str(ip) for ip in local_network().hosts()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
            results=list(pool.map(probe_hikvision,hosts))
        unique={d["ip"]:d for d in results if d}
        return list(unique.values())
    except Exception as exc:
        logging.exception("Local network discovery failed: %s",exc); return []

def discover_channels(cfg):
    urls = [f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/Streaming/channels",
            f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/ContentMgmt/StreamingProxy/channels"]
    for url in urls:
        try:
            response = requests.get(url, auth=auth(cfg), timeout=10)
            if response.status_code != 200: continue
            root = ET.fromstring(response.text); channels = []
            for node in root.iter():
                if node.tag.split("}")[-1] != "StreamingChannel": continue
                values = {}
                for child in node.iter():
                    key = child.tag.split("}")[-1]
                    if child is not node and child.text: values.setdefault(key, child.text.strip())
                channel_id = values.get("id")
                if channel_id:
                    channels.append({"channel_id": str(channel_id), "channel_name": values.get("channelName") or str(channel_id),
                                     "enabled": values.get("enabled", "true").lower() != "false",
                                     "video_input_channel_id": values.get("dynVideoInputChannelID") or values.get("videoInputChannelID")})
            enabled = [x for x in channels if x["enabled"]]
            main = [x for x in enabled if x["channel_id"].isdigit() and x["channel_id"].endswith("01")]
            selected = main or enabled; unique = {}
            for item in selected: unique.setdefault(str(item.get("video_input_channel_id") or item["channel_id"]), item)
            if unique: return list(unique.values())
        except (requests.RequestException, ET.ParseError): pass
    return []

def verify_camera(cfg):
    result = {"camera_id": cfg["camera_id"], "camera_name": cfg["camera_name"], "location": cfg["location"],
              "network": "FAILED", "camera": "FAILED", "credentials": "NOT_CHECKED", "nexusai": "EDGE_AGENT_READY",
              "verified": False, "device_type": "UNKNOWN", "channels": []}
    try:
        with socket.create_connection((cfg["camera_ip"], cfg["camera_port"]), timeout=5): result["network"] = "CONNECTED"
    except OSError as exc:
        result["error"] = f"Camera/NVR unreachable: {exc}"; return result
    try:
        xml = device_info(cfg); result["camera"] = "DETECTED"; result["credentials"] = "ACCEPTED"
        try:
            root = ET.fromstring(xml); values = {}
            for child in root.iter():
                key = child.tag.split("}")[-1]
                if child.text: values.setdefault(key, child.text.strip())
            result["device_type"] = values.get("deviceType") or "HIKVISION_DEVICE"
        except ET.ParseError: result["device_type"] = "HIKVISION_DEVICE"
        channels = discover_channels(cfg)
        if channels:
            result["channels"] = channels
            if any(t in result["device_type"].upper() for t in ("NVR", "DVR")): result["device_type"] = "NVR"
        result["verified"] = True; result["nexusai"] = "CONNECTED"
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            result["camera"] = "DETECTED"; result["credentials"] = "REJECTED"; result["error"] = "Incorrect camera/NVR username or password"
        else: result["error"] = f"Hikvision returned HTTP {exc.response.status_code if exc.response else 'ERROR'}"
    except requests.RequestException as exc: result["error"] = f"Camera/NVR request failed: {exc}"
    return result

def capture_snapshot(cfg):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        response = requests.get(f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/Streaming/channels/{cfg['snapshot_channel']}/picture",
                                auth=auth(cfg), timeout=8)
        if response.status_code != 200: return None
        filename = SNAPSHOT_DIR / f"{cfg['camera_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        filename.write_bytes(response.content); return str(filename)
    except requests.RequestException: return None

def classify_event(payload):
    lower = payload.lower()
    for keyword, definition in EVENT_NAMES.items():
        if keyword in lower: return definition
    return ("SECURITY EVENT DETECTED", "MEDIUM")

def extract_channel_id(raw_event):
    for pattern in [r"<channelID>\s*([^<]+)\s*</channelID>", r"<dynChannelID>\s*([^<]+)\s*</dynChannelID>",
                    r'"channelID"\s*:\s*"?(\d+)', r'"dynChannelID"\s*:\s*"?(\d+)']:
        match = re.search(pattern, raw_event, re.IGNORECASE)
        if match: return match.group(1).strip()
    return None

def send_event(cfg, raw_event, channel=None):
    event_name, severity = classify_event(raw_event); event_cfg = dict(cfg)
    if channel:
        event_cfg["camera_id"] = f"{cfg['camera_id']}-ch-{channel['channel_id']}"
        event_cfg["camera_name"] = channel.get("channel_name") or f"{cfg['camera_name']} CH {channel['channel_id']}"
        event_cfg["snapshot_channel"] = channel["channel_id"]
    snapshot = capture_snapshot(event_cfg)
    post_backend("/api/edge/events", {"site_id": SITE_ID, "camera_id": event_cfg["camera_id"], "camera_name": event_cfg["camera_name"],
                                      "location": event_cfg["location"], "event": event_name, "severity": severity,
                                      "timestamp": datetime.now(timezone.utc).isoformat(), "source": "nexusai-edge-agent",
                                      "snapshot_available": bool(snapshot)})

def heartbeat(cfg, verification=None):
    post_backend("/api/edge/heartbeat", {"site_id": SITE_ID, "agent_version": "1.5.0",
                                          "timestamp": datetime.now(timezone.utc).isoformat(), "status": "ONLINE",
                                          "cameras": [{"camera_id": cfg["camera_id"], "camera_name": cfg["camera_name"],
                                                       "location": cfg["location"], "verified": bool(verification and verification.get("verified"))}]})

def monitor_device(cfg, channels=None):
    session_key = f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}"
    with ACTIVE_SESSIONS_LOCK:
        if session_key in ACTIVE_SESSIONS: return False, "ALREADY_ACTIVE"
        ACTIVE_SESSIONS[session_key] = True
    channel_map = {str(c["channel_id"]): c for c in (channels or [])}
    def worker():
        url = f"http://{cfg['camera_ip']}:{cfg['camera_port']}/ISAPI/Event/notification/alertStream"
        try:
            while True:
                try:
                    with requests.get(url, auth=auth(cfg), stream=True, timeout=60) as response:
                        if response.status_code != 200: time.sleep(RECONNECT_SECONDS); continue
                        for line in response.iter_lines():
                            if not line: continue
                            decoded = line.decode("utf-8", errors="ignore")
                            if any(keyword in decoded.lower() for keyword in EVENT_NAMES):
                                send_event(cfg, decoded, channel_map.get(extract_channel_id(decoded)))
                except requests.RequestException: pass
                time.sleep(RECONNECT_SECONDS)
        finally:
            with ACTIVE_SESSIONS_LOCK: ACTIVE_SESSIONS.pop(session_key, None)
    threading.Thread(target=worker, daemon=True, name=f"nexusai-monitor-{cfg['camera_ip']}").start()
    return True, "MONITORING_STARTED"

class LocalAgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8"); self.send_response(status)
        self.send_header("Content-Type", "application/json"); self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self._send_json(204, {})
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._send_json(200, {"service":"NexusAI Edge Agent","status":"ONLINE","version":"1.5.0","site_id":SITE_ID}); return
        if path == "/pair/start":
            if not local_access_allowed(self):
                self._send_json(403, {"error":"Pairing QR generation is only allowed from the Edge Agent computer"}); return
            token = create_pair_token()
            urls = local_access_urls()
            self._send_json(200, {"service":"NexusAI Edge Agent","site_id":SITE_ID,"expires_in":PAIR_TTL_SECONDS,
                                  "agent_urls":urls,"mobile_url":urls[0]+"/mobile?token="+token}); return
        if path == "/mobile":
            mobile_path = Path(__file__).resolve().parent / "mobile.html"
            if not mobile_path.exists():
                self._send_json(404, {"error":"Mobile activation page is not installed"}); return
            body = mobile_path.read_bytes()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers()
            self.wfile.write(body); return
        if path == "/pair/status":
            self._send_json(200, {"service":"NexusAI Edge Agent","site_id":SITE_ID,"status":"ONLINE"}); return
        if path == "/discover":
            if not local_access_allowed(self) and not require_mobile_session(self): return
            self._send_json(200, {"service":"NexusAI Edge Agent","status":"ONLINE","network":"LOCAL_ONLY","devices":discover_local_devices()}); return
        self._send_json(404, {"error":"Not found"})
    def do_POST(self):
        global SITE_ID
        try:
            length = int(self.headers.get("Content-Length","0")); payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/pair/claim":
                token = str(payload.get("token","")).strip()
                session = claim_pair_token(token)
                if not session:
                    self._send_json(401, {"error":"Pairing code is invalid or expired"}); return
                self._send_json(200, {"paired":True,"site_id":SITE_ID,"session_token":session,"expires_in":SESSION_TTL_SECONDS}); return
            if self.path == "/configure":
                if not local_access_allowed(self):
                    self._send_json(403, {"error":"Site configuration is only allowed from the Edge Agent computer"}); return
                site_id = str(payload.get("site_id","")).strip()
                if not site_id or len(site_id) > 100: self._send_json(400, {"error":"Valid site ID required"}); return
                SITE_ID = site_id
                try:
                    SITE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    temp_path = SITE_CONFIG_PATH.with_suffix(".tmp")
                    temp_path.write_text(json.dumps({"site_id": SITE_ID}), encoding="utf-8")
                    temp_path.replace(SITE_CONFIG_PATH)
                except OSError as exc:
                    logging.exception("Could not persist site configuration: %s", exc)
                    self._send_json(500, {"error":"Site pairing could not be saved locally"})
                    return
                self._send_json(200, {"configured":True,"site_id":SITE_ID}); return
            if self.path not in ("/verify","/activate"):
                self._send_json(404, {"error":"Not found"}); return
            if not local_access_allowed(self) and not require_mobile_session(self):
                return
            required = ["camera_name","camera_ip","camera_port","username","password","location"]
            if any(not payload.get(key) for key in required):
                self._send_json(400, {"error":"All camera/NVR details are required"}); return
            cfg = {"camera_id":payload.get("camera_id",f"camera-{int(time.time())}"),"camera_name":str(payload["camera_name"]),
                   "camera_ip":str(payload["camera_ip"]),"camera_port":int(payload["camera_port"]),
                   "username":str(payload["username"]),"password":str(payload["password"]),"location":str(payload["location"]),
                   "snapshot_channel":str(payload.get("snapshot_channel","101"))}
            result = verify_camera(cfg)
            if result.get("verified"):
                if self.path == "/activate":
                    started, monitor_status = monitor_device(cfg,result.get("channels")); result["monitoring"]=monitor_status; result["monitoring_started"]=started
                    heartbeat(cfg,result)
                    heartbeat_key=f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}"
                    with ACTIVE_SESSIONS_LOCK:
                        if heartbeat_key not in HEARTBEAT_THREADS:
                            t=threading.Thread(target=heartbeat_loop,args=(cfg,result),daemon=True,name=f"nexusai-heartbeat-{cfg['camera_ip']}")
                            HEARTBEAT_THREADS[heartbeat_key]=t; t.start()
                post_backend("/api/edge/verify", {"site_id":SITE_ID,**result})
            self._send_json(200,result)
        except (ValueError,json.JSONDecodeError) as exc: self._send_json(400,{"error":f"Invalid request: {exc}"})
        except Exception:
            logging.exception("Local verification API error"); self._send_json(500,{"error":"Edge Agent verification failed"})
    def log_message(self,format,*args): logging.info("Local API: "+format,*args)

def start_local_api():
    server=ThreadingHTTPServer((LOCAL_AGENT_HOST,LOCAL_AGENT_PORT),LocalAgentHandler)
    if LOCAL_AGENT_TLS_CERT and LOCAL_AGENT_TLS_KEY:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(LOCAL_AGENT_TLS_CERT, LOCAL_AGENT_TLS_KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"LAN pairing API: https://0.0.0.0:{LOCAL_AGENT_PORT}")
    else:
        print(f"LAN pairing API: http://0.0.0.0:{LOCAL_AGENT_PORT} (short-lived pairing tokens required)")
    threading.Thread(target=server.serve_forever,daemon=True,name="nexusai-local-api").start()
    return server

def heartbeat_loop(cfg,verification):
    while True:
        heartbeat(cfg,verification); time.sleep(HEARTBEAT_SECONDS)

def main():
    print("="*60); print("NEXUSAI EDGE AGENT"); print("="*60)
    print(f"Site: {SITE_ID}"); print(f"Backend: {API_BASE_URL}"); print("="*60)
    start_local_api(); cfg=camera_config()
    if all([cfg["camera_ip"],cfg["username"],cfg["password"]]):
        verification=verify_camera(cfg)
        if verification.get("verified"):
            post_backend("/api/edge/verify",{"site_id":SITE_ID,**verification}); heartbeat(cfg,verification)
            threading.Thread(target=heartbeat_loop,args=(cfg,verification),daemon=True).start(); monitor_device(cfg,verification.get("channels",[]))
    while True: time.sleep(3600)

if __name__=="__main__": main()
