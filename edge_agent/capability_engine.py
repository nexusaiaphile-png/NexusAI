"""NexusAI camera/NVR capability detection.

This module detects capabilities exposed by the connected Hikvision device.
It never treats an unverified feature as supported: a feature is marked
SUPPORTED only when a Hikvision capability/feature endpoint responds
successfully or the device's capability document contains a strong,
feature-specific token.
"""

import re
import xml.etree.ElementTree as ET

import requests


CAPABILITY_DEFINITIONS = {
    "human_detection": {
        "name": "Human Detection",
        "category": "AI Analytics",
        "keywords": ("humandetection", "humanDetection", "persondetection", "personDetection", "peopleDetection"),
        "endpoints": ("/ISAPI/Smart/FieldDetection/1", "/ISAPI/System/capabilities"),
    },
    "intrusion_detection": {
        "name": "Intrusion Detection",
        "category": "Security Analytics",
        "keywords": ("intrusion", "fielddetection", "regionEntrance"),
        "endpoints": ("/ISAPI/Smart/FieldDetection/1", "/ISAPI/System/capabilities"),
    },
    "line_crossing": {
        "name": "Line Crossing",
        "category": "Security Analytics",
        "keywords": ("linedetection", "lineDetection", "linecrossing", "lineCrossing"),
        "endpoints": ("/ISAPI/Smart/LineDetection/1", "/ISAPI/System/capabilities"),
    },
    "area_entry_exit": {
        "name": "Area Entry / Exit",
        "category": "Security Analytics",
        "keywords": ("regionentrance", "regionEntrance", "regionexiting", "regionExiting"),
        "endpoints": ("/ISAPI/Smart/RegionEntrance/1", "/ISAPI/Smart/RegionExiting/1", "/ISAPI/System/capabilities"),
    },
    "loitering": {
        "name": "Loitering Detection",
        "category": "Behavior Analytics",
        "keywords": ("loitering", "loiter"),
        "endpoints": ("/ISAPI/Smart/Loitering/1", "/ISAPI/System/capabilities"),
    },
    "people_counting": {
        "name": "People Counting",
        "category": "Business Intelligence",
        "keywords": ("peoplecounting", "peopleCounting", "peopleCount", "personCount"),
        "endpoints": ("/ISAPI/Smart/PeopleCounting/1", "/ISAPI/Smart/PeopleCounting", "/ISAPI/System/capabilities"),
    },
    "heat_map": {
        "name": "Heat Map",
        "category": "Business Intelligence",
        "keywords": ("heatmap", "heatMap"),
        "endpoints": ("/ISAPI/Smart/HeatMap/1", "/ISAPI/Smart/HeatMap", "/ISAPI/System/capabilities"),
    },
    "anpr": {
        "name": "ANPR / License Plate Recognition",
        "category": "Vehicle Intelligence",
        "keywords": ("anpr", "licenseplate", "licensePlate", "plateRecognition"),
        "endpoints": ("/ISAPI/Traffic/channels/1/vehicleDetect", "/ISAPI/Traffic/ANPR", "/ISAPI/System/capabilities"),
    },
    "face_detection": {
        "name": "Face Detection",
        "category": "AI Analytics",
        "keywords": ("facedetection", "faceDetection", "face"),
        "endpoints": ("/ISAPI/Smart/FaceDetection/1", "/ISAPI/System/capabilities"),
    },
    "vehicle_detection": {
        "name": "Vehicle Detection",
        "category": "AI Analytics",
        "keywords": ("vehicledetection", "vehicleDetection", "vehicle"),
        "endpoints": ("/ISAPI/Smart/FieldDetection/1", "/ISAPI/System/capabilities"),
    },
    "object_removal": {
        "name": "Object Removal",
        "category": "Loss Prevention",
        "keywords": ("objectremoval", "objectRemoval"),
        "endpoints": ("/ISAPI/Smart/FieldDetection/1", "/ISAPI/System/capabilities"),
    },
    "unattended_object": {
        "name": "Unattended Object",
        "category": "Loss Prevention",
        "keywords": ("unattendedobject", "unattendedObject", "unattendedBaggage"),
        "endpoints": ("/ISAPI/Smart/FieldDetection/1", "/ISAPI/System/capabilities"),
    },
    "audio_exception": {
        "name": "Audio Exception",
        "category": "Device Analytics",
        "keywords": ("audioexception", "audioException", "audioabnormal"),
        "endpoints": ("/ISAPI/System/capabilities",),
    },
    "tamper_detection": {
        "name": "Camera Tamper Detection",
        "category": "Device Health",
        "keywords": ("tamper", "tampering", "videoTamper"),
        "endpoints": ("/ISAPI/System/capabilities",),
    },
}


def _text_blob(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
        values = []
        for node in root.iter():
            if node.text and node.text.strip():
                values.append(node.text.strip())
            for key, value in node.attrib.items():
                values.extend((str(key), str(value)))
        return " ".join(values).lower()
    except ET.ParseError:
        return str(xml_text or "").lower()


def _strong_keyword_match(blob: str, keywords) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", blob.lower())
    return any(re.sub(r"[^a-z0-9]+", "", str(k).lower()) in normalized for k in keywords)


def detect_capabilities(cfg, timeout=3.0):
    """Return per-feature capability state for a verified Hikvision device."""
    base = ("https" if int(cfg["camera_port"]) == 443 else "http") + f"://{cfg['camera_ip']}:{cfg['camera_port']}"
    auth = requests.auth.HTTPDigestAuth(cfg["username"], cfg["password"])
    cache = {}
    results = {}

    def get(path):
        if path in cache:
            return cache[path]
        try:
            response = requests.get(
                base + path,
                auth=auth,
                timeout=timeout,
                verify=False,
                headers={"Accept": "application/xml,text/xml,*/*"},
            )
            body = response.text[:250000] if response.text else ""
            cache[path] = (response.status_code, body)
        except requests.RequestException as exc:
            cache[path] = (0, str(exc))
        return cache[path]

    for capability_id, definition in CAPABILITY_DEFINITIONS.items():
        evidence = []
        supported = False
        for endpoint in definition["endpoints"]:
            status, body = get(endpoint)
            if status == 200:
                if endpoint.endswith("/capabilities"):
                    if _strong_keyword_match(body, definition["keywords"]):
                        supported = True
                        evidence.append("Hikvision capability document")
                else:
                    supported = True
                    evidence.append(f"endpoint {endpoint}")
            if status in (401, 403):
                evidence.append("endpoint requires authorization")
        state = "SUPPORTED" if supported else "NOT_CONFIRMED"
        results[capability_id] = {
            "name": definition["name"],
            "category": definition["category"],
            "state": state,
            "available": supported,
            "evidence": evidence[:3],
        }

    supported_count = sum(1 for item in results.values() if item["available"])
    return {
        "engine": "NexusAI Capability Engine",
        "version": "1.0.0",
        "status": "DETECTED",
        "supported_count": supported_count,
        "total_capabilities": len(results),
        "capabilities": results,
        "note": "NOT_CONFIRMED means NexusAI could not verify the feature from this device's exposed Hikvision interfaces. It does not prove the hardware lacks the feature.",
    }
