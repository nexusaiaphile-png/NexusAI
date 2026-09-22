import os
import re
import time
import logging
import smtplib
import xml.etree.ElementTree as ET

from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv


# ============================================================
# NEXUSAI AI SECURITY ENGINE
# ============================================================
# Hikvision ISAPI event listener
# Snapshot capture
# Specific security event classification
# WhatsApp alerts
# Gmail alerts
# Automatic reconnection
# ============================================================


# ====================== LOGGING ======================

logging.basicConfig(
    filename="nexusai_alerts.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ====================== LOAD ENVIRONMENT ======================

load_dotenv()

CAM_IP = os.getenv("CAM_IP")
CAM_PORT = int(os.getenv("CAM_PORT", "80"))
CAM_USER = os.getenv("CAM_USER")
CAM_PASS = os.getenv("CAM_PASS")

CAMERA_NUMBER = os.getenv("CAMERA_NUMBER", "Camera 1")
LOCATION = os.getenv("LOCATION", "Main Entrance")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TO = os.getenv("WHATSAPP_TO")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
GMAIL_TO = os.getenv("GMAIL_TO")


# ====================== HIKVISION URLS ======================

ISAPI_URL = (
    f"http://{CAM_IP}:{CAM_PORT}"
    "/ISAPI/Event/notification/alertStream"
)

SNAPSHOT_URL = (
    f"http://{CAM_IP}:{CAM_PORT}"
    "/ISAPI/Streaming/channels/101/picture"
)


# ====================== EVENT DEFINITIONS ======================

EVENT_DEFINITIONS = {

    # Critical security detections
    "weapon": {
        "name": "WEAPON DETECTED",
        "severity": "CRITICAL",
    },

    "gun": {
        "name": "WEAPON DETECTED",
        "severity": "CRITICAL",
    },

    "firearm": {
        "name": "WEAPON DETECTED",
        "severity": "CRITICAL",
    },

    "knife": {
        "name": "WEAPON DETECTED",
        "severity": "CRITICAL",
    },

    "threat": {
        "name": "THREAT DETECTED",
        "severity": "CRITICAL",
    },

    "danger": {
        "name": "THREAT DETECTED",
        "severity": "CRITICAL",
    },

    # Theft-related detections
    "theft": {
        "name": "THEFT DETECTED",
        "severity": "CRITICAL",
    },

    "stealing": {
        "name": "THEFT DETECTED",
        "severity": "CRITICAL",
    },

    "shoplifting": {
        "name": "THEFT DETECTED",
        "severity": "CRITICAL",
    },

    "objectremoval": {
        "name": "OBJECT REMOVAL DETECTED",
        "severity": "HIGH",
    },

    "object removal": {
        "name": "OBJECT REMOVAL DETECTED",
        "severity": "HIGH",
    },

    # Access/security events
    "intrusion": {
        "name": "INTRUSION DETECTED",
        "severity": "HIGH",
    },

    "intrude": {
        "name": "INTRUSION DETECTED",
        "severity": "HIGH",
    },

    "linedetection": {
        "name": "LINE CROSSING DETECTED",
        "severity": "HIGH",
    },

    "linecrossing": {
        "name": "LINE CROSSING DETECTED",
        "severity": "HIGH",
    },

    "line crossing": {
        "name": "LINE CROSSING DETECTED",
        "severity": "HIGH",
    },

    "regionentrance": {
        "name": "AREA ENTRY DETECTED",
        "severity": "HIGH",
    },

    "region entrance": {
        "name": "AREA ENTRY DETECTED",
        "severity": "HIGH",
    },

    "regionexiting": {
        "name": "AREA EXIT DETECTED",
        "severity": "MEDIUM",
    },

    "region exit": {
        "name": "AREA EXIT DETECTED",
        "severity": "MEDIUM",
    },

    "loitering": {
        "name": "LOITERING DETECTED",
        "severity": "MEDIUM",
    },

    "motion": {
        "name": "MOTION DETECTED",
        "severity": "LOW",
    },
}


# ====================== SNAPSHOT ======================

def get_snapshot():
    try:
        auth = HTTPDigestAuth(
            CAM_USER,
            CAM_PASS,
        )

        response = requests.get(
            SNAPSHOT_URL,
            auth=auth,
            timeout=5,
        )

        if response.status_code == 200:

            filename = (
                f"snapshot_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
                f".jpg"
            )

            with open(filename, "wb") as file:
                file.write(response.content)

            logging.info(
                "Snapshot saved: %s",
                filename,
            )

            return filename

        logging.warning(
            "Snapshot failed: HTTP %s",
            response.status_code,
        )

        return None

    except Exception as error:

        logging.error(
            "Snapshot error: %s",
            error,
        )

        return None


# ====================== XML CLEANING ======================

def clean_xml(raw_event):
    """
    Attempts to remove malformed leading/trailing data
    so Hikvision XML can be parsed safely.
    """

    if not raw_event:
        return ""

    cleaned = raw_event.strip()

    start = cleaned.find("<")

    if start > 0:
        cleaned = cleaned[start:]

    return cleaned


# ====================== XML EVENT EXTRACTION ======================

def extract_xml_event_type(raw_event):

    try:

        cleaned = clean_xml(raw_event)

        root = ET.fromstring(cleaned)

        for element in root.iter():

            tag = element.tag.lower()

            if tag.endswith("eventtype"):

                if element.text:
                    return element.text.strip().lower()

    except Exception:
        pass

    # Regex fallback
    try:

        match = re.search(
            r"<eventType[^>]*>(.*?)</eventType>",
            raw_event,
            re.IGNORECASE | re.DOTALL,
        )

        if match:
            return match.group(1).strip().lower()

    except Exception:
        pass

    return ""


# ====================== SECURITY CLASSIFICATION ======================

def classify_event(raw_event):

    if not raw_event:
        return {
            "name": "SECURITY EVENT DETECTED",
            "severity": "MEDIUM",
            "source": "unknown",
        }

    lower = raw_event.lower()

    # --------------------------------------------------------
    # First: use Hikvision eventType if available
    # --------------------------------------------------------

    event_type = extract_xml_event_type(raw_event)

    if event_type:

        if event_type in EVENT_DEFINITIONS:

            result = EVENT_DEFINITIONS[event_type].copy()

            result["source"] = "hikvision"

            return result

        # Partial match
        for keyword, definition in EVENT_DEFINITIONS.items():

            if keyword in event_type:

                result = definition.copy()

                result["source"] = "hikvision"

                return result

    # --------------------------------------------------------
    # Second: inspect complete event payload
    # --------------------------------------------------------

    # Weapon has priority
    for keyword in [
        "weapon",
        "firearm",
        "gun",
        "knife",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Threat
    for keyword in [
        "threat",
        "danger",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Theft
    for keyword in [
        "theft",
        "stealing",
        "shoplifting",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Object removal
    for keyword in [
        "objectremoval",
        "object removal",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Intrusion
    for keyword in [
        "intrusion",
        "intrude",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Line crossing
    for keyword in [
        "linedetection",
        "linecrossing",
        "line crossing",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Region entry
    for keyword in [
        "regionentrance",
        "region entrance",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Region exit
    for keyword in [
        "regionexiting",
        "region exit",
    ]:

        if keyword in lower:

            result = EVENT_DEFINITIONS[keyword].copy()

            result["source"] = "hikvision_event"

            return result

    # Loitering
    if "loitering" in lower:

        result = EVENT_DEFINITIONS["loitering"].copy()

        result["source"] = "hikvision_event"

        return result

    # Motion
    if "motion" in lower:

        result = EVENT_DEFINITIONS["motion"].copy()

        result["source"] = "hikvision_event"

        return result

    return {
        "name": "SECURITY EVENT DETECTED",
        "severity": "MEDIUM",
        "source": "unknown",
    }


# ====================== WHATSAPP ======================

def send_whatsapp_alert(message):

    if not all([
        WHATSAPP_TOKEN,
        WHATSAPP_PHONE_NUMBER_ID,
        WHATSAPP_TO,
    ]):

        logging.warning(
            "WhatsApp credentials missing"
        )

        return False

    url = (
        "https://graph.facebook.com/v18.0/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_TO,
        "type": "text",
        "text": {
            "body": message,
        },
    }

    try:

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=8,
        )

        if response.status_code in [200, 201]:

            logging.info(
                "WhatsApp alert sent"
            )

            print(
                "[+] WhatsApp alert sent"
            )

            return True

        logging.error(
            "WhatsApp failed: %s - %s",
            response.status_code,
            response.text,
        )

        return False

    except Exception as error:

        logging.error(
            "WhatsApp error: %s",
            error,
        )

        return False


# ====================== GMAIL ======================

def send_gmail_alert(
    subject,
    body,
    snapshot_path=None,
):

    if not all([
        GMAIL_USER,
        GMAIL_APP_PASSWORD,
        GMAIL_TO,
    ]):

        logging.warning(
            "Gmail credentials missing"
        )

        return False

    try:

        message = MIMEMultipart()

        message["From"] = GMAIL_USER
        message["To"] = GMAIL_TO
        message["Subject"] = subject

        message.attach(
            MIMEText(
                body,
                "plain",
            )
        )

        if (
            snapshot_path
            and os.path.exists(snapshot_path)
        ):

            with open(
                snapshot_path,
                "rb",
            ) as file:

                image = MIMEImage(
                    file.read()
                )

            image.add_header(
                "Content-Disposition",
                "attachment",
                filename=os.path.basename(
                    snapshot_path
                ),
            )

            message.attach(image)

        server = smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
        )

        server.login(
            GMAIL_USER,
            GMAIL_APP_PASSWORD,
        )

        server.send_message(
            message
        )

        server.quit()

        logging.info(
            "Gmail alert sent"
        )

        print(
            "[+] Gmail alert sent"
        )

        return True

    except Exception as error:

        logging.error(
            "Gmail error: %s",
            error,
        )

        return False


# ====================== ALERT MESSAGE ======================

def build_alert_message(
    detection,
    timestamp,
):

    return (
        "🚨 NEXUSAI SECURITY ALERT\n\n"
        f"DETECTION: {detection['name']}\n"
        f"SEVERITY: {detection['severity']}\n\n"
        f"TIME: {timestamp}\n"
        f"LOCATION: {LOCATION}\n"
        f"CAMERA: {CAMERA_NUMBER}\n\n"
        "NexusAI AI Security System"
    )


# ====================== PROCESS EVENT ======================

def process_event(raw_event):

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    detection = classify_event(
        raw_event
    )

    alert_message = build_alert_message(
        detection,
        timestamp,
    )

    print()
    print("=" * 55)
    print(
        f"[!] {detection['name']}"
    )
    print(
        f"[!] Severity: {detection['severity']}"
    )
    print(
        f"[!] Camera: {CAMERA_NUMBER}"
    )
    print(
        f"[!] Location: {LOCATION}"
    )
    print(
        f"[!] Time: {timestamp}"
    )
    print("=" * 55)
    print()

    logging.info(
        "Detection=%s | Severity=%s | Camera=%s | Location=%s | Source=%s",
        detection["name"],
        detection["severity"],
        CAMERA_NUMBER,
        LOCATION,
        detection["source"],
    )

    # Capture evidence
    snapshot_file = get_snapshot()

    # Send notifications
    send_whatsapp_alert(
        alert_message
    )

    send_gmail_alert(
        subject=(
            f"NexusAI - "
            f"{detection['name']} - "
            f"{CAMERA_NUMBER}"
        ),
        body=alert_message,
        snapshot_path=snapshot_file,
    )


# ====================== EVENT FILTER ======================

def is_security_event(decoded):

    lower = decoded.lower()

    security_keywords = [

        # Critical
        "weapon",
        "gun",
        "firearm",
        "knife",
        "threat",
        "danger",

        # Theft
        "theft",
        "stealing",
        "shoplifting",
        "objectremoval",
        "object removal",

        # Security
        "intrusion",
        "intrude",
        "linedetection",
        "linecrossing",
        "line crossing",
        "regionentrance",
        "region entrance",
        "regionexiting",
        "region exit",
        "loitering",

        # General
        "motion",
        "eventtype",
    ]

    return any(
        keyword in lower
        for keyword in security_keywords
    )


# ====================== HIKVISION CONNECTION ======================

def listen_to_alert_stream():

    if not all([
        CAM_IP,
        CAM_USER,
        CAM_PASS,
    ]):

        print(
            "[!] Missing camera credentials"
        )

        logging.error(
            "Missing camera credentials"
        )

        return

    auth = HTTPDigestAuth(
        CAM_USER,
        CAM_PASS,
    )

    while True:

        try:

            print(
                f"[*] Connecting to Hikvision "
                f"stream → {ISAPI_URL}"
            )

            logging.info(
                "Connecting to alert stream..."
            )

            with requests.get(
                ISAPI_URL,
                auth=auth,
                stream=True,
                timeout=60,
            ) as response:

                if response.status_code == 200:

                    print(
                        "[+] Connected to Hikvision "
                        "event stream"
                    )

                    logging.info(
                        "Successfully connected "
                        "to event stream"
                    )

                    for line in response.iter_lines():

                        if not line:
                            continue

                        decoded = line.decode(
                            "utf-8",
                            errors="ignore",
                        )

                        if is_security_event(
                            decoded
                        ):

                            process_event(
                                decoded
                            )

                elif response.status_code == 401:

                    print(
                        "[!] Hikvision authentication "
                        "failed - check username/password"
                    )

                    logging.error(
                        "Hikvision authentication failed: 401"
                    )

                    time.sleep(10)

                else:

                    print(
                        "[!] Hikvision stream returned "
                        f"HTTP {response.status_code}"
                    )

                    logging.warning(
                        "Stream status: %s",
                        response.status_code,
                    )

                    time.sleep(10)

        except requests.exceptions.Timeout:

            print(
                "[!] Hikvision connection timed out. "
                "Reconnecting in 10 seconds..."
            )

            logging.error(
                "Hikvision connection timed out"
            )

            time.sleep(10)

        except requests.exceptions.ConnectionError as error:

            print(
                "[!] Hikvision connection failed: "
                f"{error}. Reconnecting in 10 seconds..."
            )

            logging.error(
                "Hikvision connection error: %s",
                error,
            )

            time.sleep(10)

        except requests.exceptions.RequestException as error:

            print(
                "[!] Hikvision request error: "
                f"{error}. Reconnecting in 10 seconds..."
            )

            logging.error(
                "Hikvision request error: %s",
                error,
            )

            time.sleep(10)

        except Exception as error:

            print(
                "[!] Unexpected error: "
                f"{error}. Reconnecting in 10 seconds..."
            )

            logging.exception(
                "Unexpected error"
            )

            time.sleep(10)


# ====================== START ======================

if __name__ == "__main__":

    print()
    print("=" * 55)
    print("          NEXUSAI AI SECURITY ENGINE")
    print("=" * 55)
    print(f"Camera:   {CAMERA_NUMBER}")
    print(f"Location: {LOCATION}")
    print("=" * 55)
    print()

    listen_to_alert_stream()
