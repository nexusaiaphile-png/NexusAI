import json
import hashlib
import logging
import os
import socket
import time
import ipaddress
import concurrent.futures
import uuid

import threading
import re
import ssl
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("NEXUSAI_API_URL", "https://getnexusai.co.za").rstrip("/")
EDGE_AGENT_TOKEN = os.getenv("NEXUSAI_EDGE_TOKEN", "")
EDGE_AGENT_VERSION = "1.9.1"
MAX_MONITORS = int(os.getenv("NEXUSAI_MAX_MONITORS", "32"))
SNAPSHOT_MAX_BYTES = int(os.getenv("NEXUSAI_SNAPSHOT_MAX_BYTES", str(2 * 1024 * 1024)))
SCAN_SUBNETS = [x.strip() for x in os.getenv("NEXUSAI_SCAN_SUBNETS", "").split(",") if x.strip()]
SITE_ID = os.getenv("NEXUSAI_SITE_ID", "site-unknown")
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "30"))
RECONNECT_SECONDS = int(os.getenv("RECONNECT_SECONDS", "10"))
AUTO_UPDATE = os.getenv("NEXUSAI_AUTO_UPDATE", "true").strip().lower() in {"1","true","yes","on"}
UPDATE_CHECK_SECONDS = max(300, int(os.getenv("NEXUSAI_UPDATE_CHECK_SECONDS", "900")))
UPDATE_BASE_URL = os.getenv("NEXUSAI_UPDATE_BASE_URL", "https://getnexusai.co.za").rstrip("/")
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
LOG_FILE = os.getenv("LOG_FILE", "nexusai_edge.log")
LOCAL_AGENT_HOST = os.getenv("LOCAL_AGENT_HOST", "127.0.0.1")
LOCAL_AGENT_PORT = int(os.getenv("LOCAL_AGENT_PORT", "8787"))
SITE_CONFIG_PATH = Path(os.getenv("SITE_CONFIG_PATH", str(Path.home() / ".nexusai_site.json")))
LOCAL_ALLOWED_ORIGINS = {"https://getnexusai.co.za", "https://www.getnexusai.co.za"}
SADP_MULTICAST = "239.255.255.250"
SADP_PORT = 37020
SADP_TIMEOUT_SECONDS = 2.5
MAX_SCAN_HOSTS = 4096
DISCOVERY_LOCK = threading.Lock()

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
MONITOR_STATUS = {}
ACTIVE_CONFIGS = {}
ACTIVE_SESSIONS_LOCK = threading.Lock()
HEARTBEAT_THREADS = {}
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
            "User-Agent": f"NexusAI-Edge-Agent/{EDGE_AGENT_VERSION}"}

def post_backend(path, payload):
    if not EDGE_AGENT_TOKEN:
        logging.error("NEXUSAI_EDGE_TOKEN is missing")
        return False
    for attempt in range(5):
        try:
            response = requests.post(f"{API_BASE_URL}{path}", json=payload, headers=headers(), timeout=12)
            if 200 <= response.status_code < 300:
                return True
            if response.status_code in (401, 403):
                logging.error("NexusAI cloud authentication failed: HTTP %s", response.status_code)
                return False
            logging.warning("Backend %s returned HTTP %s: %s", path, response.status_code, response.text[:300])
        except requests.RequestException as exc:
            logging.warning("Backend connection failed (attempt %s/5): %s", attempt + 1, exc)
        if attempt < 4:
            time.sleep(min(8, 2 ** attempt))
    return False

def device_info(cfg):
    response = requests.get(f"{hikvision_base_url(cfg)}/ISAPI/System/deviceInfo",
                            auth=auth(cfg), timeout=8)
    response.raise_for_status()
    return response.text

def local_ipv4_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(item[4][0])
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                addresses.add(address)
        finally:
            sock.close()
    except OSError:
        pass
    return sorted(addresses)

def local_network():
    addresses = local_ipv4_addresses()
    if not addresses:
        raise OSError("No local IPv4 interface was found.")
    return ipaddress.ip_network(f"{addresses[0]}/24", strict=False)

def resolve_scan_networks(requested_subnet=None):
    candidates = []
    if requested_subnet:
        candidates.append(requested_subnet.strip())
    candidates.extend(SCAN_SUBNETS)
    if not candidates:
        for address in local_ipv4_addresses():
            candidates.append(str(ipaddress.ip_network(f"{address}/24", strict=False)))
    if not candidates:
        try:
            candidates.append(str(local_network()))
        except Exception:
            pass
    networks = []
    for value in candidates:
        try:
            network = ipaddress.ip_network(value, strict=False)
            if network.version == 4 and network.prefixlen >= 16:
                networks.append(network)
        except ValueError:
            logging.warning("Ignoring invalid NexusAI scan subnet: %s", value)
    unique = {}
    for network in networks:
        unique[str(network)] = network
    return list(unique.values())

def _xml_text(root, key, default=""):
    for node in root.iter():
        if node.tag.split("}")[-1] == key and node.text:
            return node.text.strip()
    return default

def discover_sadp_devices(timeout=SADP_TIMEOUT_SECONDS):
    """Discover Hikvision devices using the LAN SADP multicast protocol."""
    probe = ('<?xml version="1.0" encoding="utf-8"?>'
             '<Probe><Uuid>' + str(uuid.uuid4()) + '</Uuid><Types>inquiry</Types></Probe>').encode("utf-8")
    found = {}
    addresses = local_ipv4_addresses() or [""]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(float(timeout))
        for address in addresses:
            try:
                if address:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                sock.sendto(probe, (SADP_MULTICAST, SADP_PORT))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(probe, ("255.255.255.255", SADP_PORT))
            except OSError:
                continue
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            try:
                raw, peer = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            if root.tag.split("}")[-1] != "ProbeMatch":
                continue
            ip = _xml_text(root, "IPv4Address", peer[0] if peer else "")
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            try:
                http_port = int(_xml_text(root, "HttpPort", "80") or 80)
            except ValueError:
                http_port = 80
            try:
                command_port = int(_xml_text(root, "CommandPort", "8000") or 8000)
            except ValueError:
                command_port = 8000
            found[ip] = {
                "name": _xml_text(root, "DeviceDescription", "Hikvision device"),
                "ip": ip,
                "port": http_port,
                "command_port": command_port,
                "type": "Hikvision",
                "device_type": _xml_text(root, "DeviceType", ""),
                "serial": _xml_text(root, "DeviceSN", ""),
                "mac": _xml_text(root, "MAC", ""),
                "discovery": "SADP",
            }
    except OSError as exc:
        logging.warning("Hikvision SADP discovery unavailable: %s", exc)
    finally:
        sock.close()
    return list(found.values())

def probe_hikvision(ip, ports=(80, 443)):
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=0.45):
                scheme = "https" if port == 443 else "http"
                response = requests.get(
                    f"{scheme}://{ip}/ISAPI/System/deviceInfo",
                    timeout=1.5,
                    allow_redirects=False,
                    verify=False,
                )
                server = (response.headers.get("Server") or "").lower()
                body = response.text[:4000].lower()
                if response.status_code == 401 or "hikvision" in server or "hikvision" in body:
                    return {"name":"Hikvision device","ip":ip,"port":port,"type":"Hikvision","discovery":"LOCAL_NETWORK"}
        except (OSError, requests.RequestException):
            continue
    return None

def discover_local_devices(requested_subnet=None, manual_ip=None, manual_port=None):
    try:
        if manual_ip:
            try:
                ipaddress.ip_address(manual_ip)
            except ValueError:
                return []
            device = probe_hikvision(manual_ip, (int(manual_port or 80),))
            return [device] if device else []

        unique = {d["ip"]: d for d in discover_sadp_devices() if d.get("ip")}
        networks = resolve_scan_networks(requested_subnet)
        hosts = []
        for network in networks:
            if network.num_addresses > MAX_SCAN_HOSTS:
                local_addresses = local_ipv4_addresses()
                narrowed = None
                for address in local_addresses:
                    try:
                        candidate = ipaddress.ip_network(f"{address}/24", strict=False)
                        if candidate.subnet_of(network):
                            narrowed = candidate
                            break
                    except ValueError:
                        continue
                if narrowed:
                    hosts.extend(str(ip) for ip in narrowed.hosts())
                else:
                    logging.info("Skipping broad scan %s; use SADP or configure NEXUSAI_SCAN_SUBNETS with a smaller range.", network)
                continue
            hosts.extend(str(ip) for ip in network.hosts())
        hosts = list(dict.fromkeys(hosts))
        if hosts:
            with concurrent.futures.ThreadPoolExecutor(max_workers=96) as pool:
                results = list(pool.map(probe_hikvision, hosts))
            for device in results:
                if device:
                    unique.setdefault(device["ip"], device)
        return list(unique.values())
    except Exception as exc:
        logging.exception("Local network discovery failed: %s", exc)
        return []

def hikvision_base_url(cfg):
    scheme = "https" if int(cfg["camera_port"]) == 443 else "http"
    return f"{scheme}://{cfg['camera_ip']}:{cfg['camera_port']}"

def discover_channels(cfg):
    base = hikvision_base_url(cfg)
    urls = [f"{base}/ISAPI/Streaming/channels",
            f"{base}/ISAPI/ContentMgmt/StreamingProxy/channels"]
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
            # Hikvision NVR channel IDs are not universally numbered as 101/201/301.
            # For example, Hikvision documents valid NVR channels such as 104 and
            # 1301. Group by the physical/video-input channel and prefer a primary
            # stream ending in 01 when one exists; otherwise keep the first enabled
            # stream for that camera.
            grouped = {}
            for item in enabled:
                camera_key = str(item.get("video_input_channel_id") or item["channel_id"])
                current = grouped.get(camera_key)
                if current is None:
                    grouped[camera_key] = item
                else:
                    current_id = str(current.get("channel_id") or "")
                    candidate_id = str(item.get("channel_id") or "")
                    current_primary = current_id.isdigit() and current_id.endswith("01")
                    candidate_primary = candidate_id.isdigit() and candidate_id.endswith("01")
                    if candidate_primary and not current_primary:
                        grouped[camera_key] = item
            if grouped: return list(grouped.values())
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
    try:
        response = requests.get(
            f"{hikvision_base_url(cfg)}/ISAPI/Streaming/channels/{cfg['snapshot_channel']}/picture",
            auth=auth(cfg), timeout=8
        )
        if response.status_code != 200 or not response.content or len(response.content) > SNAPSHOT_MAX_BYTES:
            return None
        import base64
        return {
            "base64": base64.b64encode(response.content).decode("ascii"),
            "mime": response.headers.get("Content-Type", "image/jpeg").split(";")[0],
            "filename": f"{cfg['camera_id']}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg",
        }
    except requests.RequestException as exc:
        logging.warning("Snapshot capture failed: %s", exc)
        return None

def _xml_values(raw_event):
    values = {}
    try:
        root = ET.fromstring(raw_event)
        for node in root.iter():
            key = node.tag.split("}")[-1]
            if node.text and node.text.strip():
                values.setdefault(key, node.text.strip())
    except ET.ParseError:
        pass
    return values

def parse_hikvision_event(raw_event):
    values = _xml_values(raw_event)
    channel_id = values.get("channelID") or values.get("dynChannelID") or values.get("videoInputChannelID") or values.get("channelId")
    event_type = values.get("eventType") or values.get("eventState") or values.get("eventDescription") or ""
    active_status = (values.get("activeStatus") or "").lower()
    description = " ".join(str(values.get(k, "")) for k in (
        "eventType","eventDescription","eventDesc","eventState","DetectionRegion","objectType","human","vehicle","targetType"
    ))
    haystack = (description + " " + raw_event).lower()
    return {
        "channel_id": str(channel_id).strip() if channel_id else None,
        "event_type": str(event_type).strip(),
        "active": active_status not in {"inactive","false","0","ended","stop"},
        "description": description.strip(),
        "haystack": haystack,
        "values": values,
    }

def classify_event(payload):
    parsed = payload if isinstance(payload, dict) else parse_hikvision_event(payload)
    lower = parsed.get("haystack", str(payload)).lower()
    for keyword, definition in EVENT_NAMES.items():
        if keyword in lower:
            return definition
    event_type = str(parsed.get("event_type", "")).lower()
    if "vmd" in event_type or "motion" in event_type:
        return ("MOTION DETECTED", "LOW")
    if "regionentrance" in event_type or "fielddetection" in event_type:
        return ("AREA ENTRY DETECTED", "HIGH")
    if "regionexiting" in event_type:
        return ("AREA EXIT DETECTED", "MEDIUM")
    if "linedetection" in event_type:
        return ("LINE CROSSING DETECTED", "HIGH")
    if "loiter" in event_type:
        return ("LOITERING DETECTED", "MEDIUM")
    if "objectremoval" in event_type or "unattended" in event_type:
        return ("OBJECT SECURITY EVENT", "HIGH")
    return ("SECURITY EVENT DETECTED", "MEDIUM")

def extract_channel_id(raw_event):
    return parse_hikvision_event(raw_event).get("channel_id")

def send_event(cfg, raw_event, channel=None, parsed=None):
    parsed = parsed or parse_hikvision_event(raw_event)
    if not parsed.get("active", True):
        return
    event_name, severity = classify_event(parsed)
    event_cfg = dict(cfg)
    channel_id = parsed.get("channel_id")
    if channel:
        channel_id = str(channel.get("channel_id") or channel_id or "")
        event_cfg["camera_id"] = f"{cfg['camera_id']}-ch-{channel_id}"
        event_cfg["camera_name"] = channel.get("channel_name") or f"{cfg['camera_name']} CH {channel_id}"
        event_cfg["snapshot_channel"] = channel_id
    elif channel_id:
        event_cfg["camera_id"] = f"{cfg['camera_id']}-ch-{channel_id}"
        event_cfg["camera_name"] = f"{cfg['camera_name']} CH {channel_id}"
        event_cfg["snapshot_channel"] = channel_id
    snapshot = capture_snapshot(event_cfg)
    payload = {
        "site_id": SITE_ID, "camera_id": event_cfg["camera_id"], "camera_name": event_cfg["camera_name"],
        "location": event_cfg["location"], "event": event_name, "severity": severity,
        "timestamp": parsed["values"].get("dateTime") or datetime.now(timezone.utc).isoformat(),
        "source": "nexusai-edge-agent", "snapshot_available": bool(snapshot)
    }
    if snapshot:
        payload.update({
            "snapshot_base64": snapshot["base64"],
            "snapshot_mime": snapshot["mime"],
            "snapshot_filename": snapshot["filename"],
        })
    post_backend("/api/edge/events", payload)

def heartbeat(cfg=None, verification=None):
    cameras = []
    if cfg is not None:
        channels = (verification or {}).get("channels") or ACTIVE_CONFIGS.get(
            f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}", {}
        ).get("channels", [])
        for channel in channels:
            cameras.append({
                "camera_id": f"{cfg['camera_id']}-ch-{channel.get('channel_id')}",
                "camera_name": channel.get("channel_name") or f"{cfg['camera_name']} CH {channel.get('channel_id')}",
                "location": cfg["location"], "verified": True,
                "device_id": f"{cfg['camera_ip']}:{cfg['camera_port']}", "device_ip": cfg["camera_ip"],
                "channel_id": str(channel.get("channel_id")), "device_type": verification.get("device_type") if verification else "HIKVISION_DEVICE",
            })
        if not cameras:
            cameras.append({"camera_id":cfg["camera_id"],"camera_name":cfg["camera_name"],"location":cfg["location"],
                             "verified":bool(verification and verification.get("verified")),"device_id":f"{cfg['camera_ip']}:{cfg['camera_port']}",
                             "device_ip":cfg["camera_ip"],"channel_id":None,"device_type":verification.get("device_type") if verification else "HIKVISION_DEVICE"})
    else:
        for session_key,item in ACTIVE_CONFIGS.items():
            monitor_state = MONITOR_STATUS.get(session_key, {})
            state = monitor_state.get("status", "ONLINE")
            for camera in item.get("cameras", []):
                camera_copy = dict(camera)
                camera_copy["status"] = "ONLINE" if state == "ONLINE" else "OFFLINE"
                cameras.append(camera_copy)
    post_backend("/api/edge/heartbeat", {
        "site_id": SITE_ID, "agent_version": EDGE_AGENT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(), "status": "ONLINE", "cameras": cameras
    })

def _event_fragments(response):
    buffer = []
    in_xml = False
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None: continue
        line = str(raw_line).strip()
        if not line: continue
        if "<EventNotificationAlert" in line:
            in_xml = True
            buffer = [line[line.find("<EventNotificationAlert"):]]
        elif in_xml:
            buffer.append(line)
        if in_xml and "</EventNotificationAlert>" in line:
            joined = "\n".join(buffer)
            end = joined.find("</EventNotificationAlert>") + len("</EventNotificationAlert>")
            yield joined[:end]
            buffer = []
            in_xml = False

def monitor_device(cfg, channels=None):
    session_key = f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}"
    with ACTIVE_SESSIONS_LOCK:
        if session_key in ACTIVE_SESSIONS: return False, "ALREADY_ACTIVE"
        if len(ACTIVE_SESSIONS) >= MAX_MONITORS: return False, "MONITOR_LIMIT_REACHED"
        ACTIVE_SESSIONS[session_key] = True
        ACTIVE_CONFIGS[session_key] = {"cfg":dict(cfg),"channels":list(channels or []),"cameras":[]}
    channel_map = {str(c["channel_id"]): c for c in (channels or [])}
    for channel in (channels or []):
        ACTIVE_CONFIGS[session_key]["cameras"].append({
            "camera_id": f"{cfg['camera_id']}-ch-{channel.get('channel_id')}",
            "camera_name": channel.get("channel_name") or f"{cfg['camera_name']} CH {channel.get('channel_id')}",
            "location": cfg["location"], "verified": True,
            "device_id": f"{cfg['camera_ip']}:{cfg['camera_port']}", "device_ip": cfg["camera_ip"],
            "channel_id": str(channel.get("channel_id")), "device_type": "HIKVISION_DEVICE",
        })
    def worker():
        reconnect_delay = RECONNECT_SECONDS
        try:
            while True:
                try:
                    url = f"{hikvision_base_url(cfg)}/ISAPI/Event/notification/alertStream"
                    MONITOR_STATUS[session_key] = {"status":"CONNECTING","last_event":MONITOR_STATUS.get(session_key,{}).get("last_event")}
                    with requests.get(url, auth=auth(cfg), stream=True, timeout=(10,90)) as response:
                        if response.status_code != 200: raise requests.RequestException(f"alertStream HTTP {response.status_code}")
                        reconnect_delay = RECONNECT_SECONDS
                        MONITOR_STATUS[session_key] = {"status":"ONLINE","last_event":MONITOR_STATUS.get(session_key,{}).get("last_event")}
                        for fragment in _event_fragments(response):
                            parsed = parse_hikvision_event(fragment)
                            if not parsed.get("active", True): continue
                            parsed_channel = parsed.get("channel_id")
                            if channel_map and (not parsed_channel or parsed_channel not in channel_map):
                                continue
                            fingerprint = "|".join([parsed.get("event_type",""),parsed_channel or "",
                                                     parsed.get("values",{}).get("dateTime",""),parsed.get("description","")])
                            event_key = hashlib.sha256(fingerprint.lower().encode()).hexdigest()
                            now=time.time()
                            recent={k:v for k,v in getattr(worker,"_recent_events",{}).items() if now-v<10}
                            if event_key in recent: continue
                            recent[event_key]=now; worker._recent_events=recent
                            send_event(cfg,fragment,channel_map.get(parsed.get("channel_id")),parsed)
                            MONITOR_STATUS[session_key]={"status":"ONLINE","last_event":datetime.now(timezone.utc).isoformat()}
                except (requests.RequestException,ET.ParseError,OSError) as exc:
                    MONITOR_STATUS[session_key]={"status":"RECONNECTING","error":str(exc)[:240],"last_event":MONITOR_STATUS.get(session_key,{}).get("last_event")}
                    logging.warning("Hikvision monitor %s reconnecting: %s",session_key,exc)
                except Exception as exc:
                    MONITOR_STATUS[session_key]={"status":"ERROR","error":str(exc)[:240],"last_event":MONITOR_STATUS.get(session_key,{}).get("last_event")}
                    logging.exception("Unexpected Hikvision monitor failure: %s",exc)
                time.sleep(reconnect_delay)
                reconnect_delay=min(60,max(RECONNECT_SECONDS,reconnect_delay*2))
        finally:
            with ACTIVE_SESSIONS_LOCK:
                ACTIVE_SESSIONS.pop(session_key,None); ACTIVE_CONFIGS.pop(session_key,None)
            MONITOR_STATUS.pop(session_key,None)
    threading.Thread(target=worker,daemon=True,name=f"nexusai-monitor-{cfg['camera_ip']}").start()
    return True,"MONITORING_STARTED"

class LocalAgentHandler(BaseHTTPRequestHandler):
    MAX_BODY_BYTES = 256 * 1024

    def _origin_allowed(self):
        origin = self.headers.get("Origin", "")
        return not origin or origin in LOCAL_ALLOWED_ORIGINS

    def _local_request_allowed(self):
        host = (self.client_address[0] if self.client_address else "").split("%", 1)[0]
        return host in {"127.0.0.1", "::1", "localhost"}

    def _send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "https://getnexusai.co.za")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NexusAI-Local")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self._local_request_allowed() or not self._origin_allowed():
            self.send_response(403); self.end_headers(); return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "https://getnexusai.co.za")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NexusAI-Local")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):
        if not self._local_request_allowed() or not self._origin_allowed():
            self._send_json(403, {"error":"Local Edge Agent access is restricted to the site computer."}); return
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._send_json(200, {"service":"NexusAI Edge Agent","status":"ONLINE","version":EDGE_AGENT_VERSION,"site_id":SITE_ID}); return
        if path == "/pair/status":
            self._send_json(200, {"service":"NexusAI Edge Agent","site_id":SITE_ID,"status":"ONLINE"}); return
        if path == "/inventory":
            self._send_json(200, local_inventory()); return
        if path == "/setup/status":
            username = os.getenv("CAM_USER", "").strip()
            password = os.getenv("CAM_PASS", "")
            self._send_json(200, {"service":"NexusAI Security Box","status":"ONLINE","site_id":SITE_ID,
                                  "hikvision_credentials_saved":bool(username and password),
                                  "scan_subnets":[str(x) for x in resolve_scan_networks()]}); return
        if path == "/discover":
            params = {k:v[0] for k,v in parse_qs(query, keep_blank_values=True).items()}
            devices = discover_local_devices(params.get("subnet"), params.get("ip"), params.get("port"))
            self._send_json(200, {"service":"NexusAI Edge Agent","status":"ONLINE","network":"LOCAL_ONLY","subnets":[str(x) for x in resolve_scan_networks(params.get("subnet"))],"devices":devices}); return
        self._send_json(404, {"error":"Not found"})

    def do_POST(self):
        global SITE_ID
        try:
            if not self._local_request_allowed() or not self._origin_allowed():
                self._send_json(403, {"error":"Local Edge Agent access is restricted to the site computer."}); return
            length = int(self.headers.get("Content-Length","0"))
            if length <= 0 or length > self.MAX_BODY_BYTES:
                self._send_json(413, {"error":"Request body is missing or too large."}); return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/configure":
                site_id = str(payload.get("site_id","")).strip()
                if not re.match(r"^site-[A-Za-z0-9._-]{6,100}$", site_id):
                    self._send_json(400, {"error":"Valid NexusAI site ID required"}); return
                SITE_ID = site_id
                SITE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp_path = SITE_CONFIG_PATH.with_suffix(".tmp")
                temp_path.write_text(json.dumps({"site_id":SITE_ID}), encoding="utf-8")
                temp_path.replace(SITE_CONFIG_PATH)
                self._send_json(200, {"configured":True,"site_id":SITE_ID}); return
            if self.path == "/credentials":
                username = str(payload.get("username","")).strip()[:128]
                password = str(payload.get("password",""))
                location = str(payload.get("location","Security Site")).strip()[:200] or "Security Site"
                if not username or not password:
                    self._send_json(400, {"error":"Hikvision username and password are required."}); return
                try:
                    requested_ip = str(payload.get("nvr_ip","")).strip()
                    requested_port = int(payload.get("nvr_port") or 80)
                    if requested_ip:
                        ipaddress.ip_address(requested_ip)
                        if not 1 <= requested_port <= 65535:
                            raise ValueError
                    secure_store.set_hikvision_credentials(SITE_ID, username, password)
                    os.environ["CAM_USER"] = username
                    os.environ["CAM_PASS"] = password
                    os.environ["LOCATION"] = location
                except ValueError:
                    self._send_json(400, {"error":"The NVR IP address or port is invalid."}); return
                except Exception:
                    logging.exception("Could not save Hikvision credentials")
                    self._send_json(500, {"error":"NexusAI could not securely save the Hikvision credentials."}); return
                threading.Thread(target=discover_with_credentials,
                                 args=(username,password,location,requested_ip,requested_port),
                                 daemon=True,name="nexusai-immediate-discovery").start()
                self._send_json(200, {"saved":True,"site_id":SITE_ID,"status":"SEARCHING"}); return
            if self.path not in ("/verify","/activate"):
                self._send_json(404, {"error":"Not found"}); return
            required = ["camera_name","camera_ip","camera_port","username","password","location"]
            if any(not payload.get(key) for key in required):
                self._send_json(400, {"error":"All camera/NVR details are required"}); return
            try:
                camera_port=int(payload["camera_port"]); ipaddress.ip_address(str(payload["camera_ip"]))
                if not 1 <= camera_port <= 65535: raise ValueError
            except ValueError:
                self._send_json(400, {"error":"Invalid camera IP address or port"}); return
            cfg={"camera_id":payload.get("camera_id",f"camera-{int(time.time())}"),"camera_name":str(payload["camera_name"])[:200],"camera_ip":str(payload["camera_ip"]),"camera_port":camera_port,"username":str(payload["username"])[:128],"password":str(payload["password"]),"location":str(payload["location"])[:200],"snapshot_channel":str(payload.get("snapshot_channel","101"))[:32]}
            requested_channels=payload.get("channels") or []
            if not isinstance(requested_channels,list) or len(requested_channels)>256:
                self._send_json(400, {"error":"channels must be a list with at most 256 entries"}); return
            result=verify_camera(cfg)
            if result.get("verified"):
                if self.path == "/activate":
                    available=result.get("channels") or []
                    if requested_channels:
                        allowed={str(x.get("channel_id")) for x in requested_channels if isinstance(x,dict)}
                        result["channels"]=[x for x in available if str(x.get("channel_id")) in allowed]
                    if not result.get("channels"):
                        self._send_json(400, {"error":"No camera channels were selected."}); return
                    started,monitor_status=monitor_device(cfg,result.get("channels")); result["monitoring"]=monitor_status; result["monitoring_started"]=started
                    heartbeat(cfg,result)
                post_backend("/api/edge/verify", {"site_id":SITE_ID,**result})
            self._send_json(200,result)
        except (ValueError,json.JSONDecodeError) as exc:
            self._send_json(400,{"error":f"Invalid request: {exc}"})
        except Exception:
            logging.exception("Local verification API error"); self._send_json(500,{"error":"Edge Agent verification failed"})

    def log_message(self, format, *args):
        logging.info("Local API: " + format, *args)

def local_inventory():
    """Return real, authorized local security-service state for the portal/diagnostics."""
    with ACTIVE_SESSIONS_LOCK:
        active = list(ACTIVE_SESSIONS.keys())
    return {"service":"NexusAI Local Security Service","version":EDGE_AGENT_VERSION,"site_id":SITE_ID,"status":"ONLINE","active_monitors":len(active),"max_monitors":MAX_MONITORS,"monitors":MONITOR_STATUS}

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

def _version_tuple(value):
    match = re.search(r"(\\d+)\\.(\\d+)\\.(\\d+)", str(value or ""))
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)


def check_for_agent_update():
    if not AUTO_UPDATE or getattr(sys, "frozen", False):
        return False
    try:
        source_path = Path(__file__).resolve()
        if not source_path.exists() or not os.access(source_path, os.W_OK):
            logging.info("Automatic update skipped: Edge Agent installation is not writable.")
            return False
        response = requests.get(f"{UPDATE_BASE_URL}/downloads/edge-agent/agent.py", timeout=20)
        response.raise_for_status()
        remote = response.text
        if len(remote) > 2 * 1024 * 1024:
            raise ValueError("Remote Edge Agent update is unexpectedly large.")
        compile(remote, "nexusai-edge-agent-update", "exec")
        match = re.search(r'EDGE_AGENT_VERSION\\s*=\\s*"([^"]+)"', remote)
        remote_version = match.group(1) if match else ""
        if not remote_version or _version_tuple(remote_version) <= _version_tuple(EDGE_AGENT_VERSION):
            return False
        temp_path = source_path.with_suffix(".update")
        temp_path.write_text(remote, encoding="utf-8")
        os.replace(temp_path, source_path)
        logging.info("NexusAI Edge Agent updated from %s to %s; restarting.", EDGE_AGENT_VERSION, remote_version)
        os.execv(sys.executable, [sys.executable, str(source_path)])
        return True
    except Exception as exc:
        logging.warning("Automatic Edge Agent update check failed: %s", exc)
        return False


def auto_update_loop():
    while True:
        time.sleep(UPDATE_CHECK_SECONDS)
        try:
            check_for_agent_update()
        except Exception:
            logging.exception("NexusAI automatic update loop failed")


def heartbeat_loop():
    while True:
        try: heartbeat()
        except Exception: logging.exception("NexusAI aggregate heartbeat failed")
        time.sleep(HEARTBEAT_SECONDS)

def discover_with_credentials(username, password, location="Security Site", manual_ip="", manual_port=80):
    """Run one guarded discovery pass and start monitors for verified Hikvision devices."""
    if not username or not password:
        return []
    if not DISCOVERY_LOCK.acquire(blocking=False):
        return []
    verified_devices = []
    try:
        devices = discover_local_devices(manual_ip=manual_ip, manual_port=manual_port) if manual_ip else discover_local_devices()
        for device in devices:
            if str(device.get("type","")).lower() != "hikvision":
                continue
            ip = str(device.get("ip","")).strip()
            if not ip:
                continue
            cfg = {"camera_id":f"nvr-{ip.replace('.','-')}","camera_name":device.get("name") or "Hikvision NVR",
                   "camera_ip":ip,"camera_port":int(device.get("port") or 80),"username":username,
                   "password":password,"location":location,"snapshot_channel":"101"}
            session_key = f"{cfg['camera_ip']}:{cfg['camera_port']}:{cfg['username']}"
            if session_key in ACTIVE_SESSIONS:
                continue
            verification = verify_camera(cfg)
            if verification.get("verified"):
                verified_devices.append(verification)
                post_backend("/api/edge/verify", {"site_id":SITE_ID, **verification})
                monitor_device(cfg, verification.get("channels", []))
        if verified_devices:
            heartbeat()
    except Exception:
        logging.exception("NexusAI credentialed discovery failed")
    finally:
        DISCOVERY_LOCK.release()
    return verified_devices

def autonomous_discovery_loop():
    """Continuously discover local Hikvision devices and attach configured credentials."""
    while True:
        try:
            username = os.getenv("CAM_USER", "").strip()
            password = os.getenv("CAM_PASS", "")
            location = os.getenv("LOCATION", "Security Site")
            if username and password:
                discover_with_credentials(username, password, location)
        except Exception:
            logging.exception("NexusAI autonomous discovery failed")
        time.sleep(max(30, RECONNECT_SECONDS))

def main():
    print("="*60); print("NEXUSAI EDGE AGENT"); print("="*60)
    print(f"Site: {SITE_ID}"); print(f"Backend: {API_BASE_URL}"); print(f"Version: {EDGE_AGENT_VERSION}"); print("="*60)
    start_local_api()
    threading.Thread(target=heartbeat_loop,daemon=True,name="nexusai-aggregate-heartbeat").start()
    threading.Thread(target=autonomous_discovery_loop,daemon=True,name="nexusai-autonomous-discovery").start()
    threading.Thread(target=auto_update_loop,daemon=True,name="nexusai-auto-updater").start()
    cfg=camera_config()
    if all([cfg["camera_ip"],cfg["username"],cfg["password"]]):
        verification=verify_camera(cfg)
        if verification.get("verified"):
            post_backend("/api/edge/verify",{"site_id":SITE_ID,**verification})
            monitor_device(cfg,verification.get("channels",[]))
    while True: time.sleep(3600)

if __name__=="__main__": main()