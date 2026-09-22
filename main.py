import os
import re
import time
import logging
import smtplib
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

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

ISAPI_URL = f"http://{CAM_IP}:{CAM_PORT}/ISAPI/Event/notification/alertStream"
SNAPSHOT_URL = f"http://{CAM_IP}:{CAM_PORT}/ISAPI/Streaming/channels/101/picture"


def get_snapshot():
    try:
        auth = HTTPDigestAuth(CAM_USER, CAM_PASS)
        response = requests.get(SNAPSHOT_URL, auth=auth, timeout=5)

        if response.status_code == 200:
            filename = f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

            with open(filename, "wb") as f:
                f.write(response.content)

            logging.info("Snapshot saved: %s", filename)
            return filename

        logging.warning("Snapshot failed: %s", response.status_code)
        return None

    except Exception as e:
        logging.error("Snapshot error: %s", e)
        return None


def extract_event_type(raw_xml):
    """Try to extract a clean event type from the camera XML."""
    try:
        match = re.search(r"<eventType>(.*?)</eventType>", raw_xml, re.IGNORECASE)

        if match:
            return match.group(1).strip()

        keywords = {
            "intrusion": "Intrusion Detected",
            "linedetection": "Line Crossing",
            "regionentrance": "Region Entrance",
            "regionexiting": "Region Exit",
            "loitering": "Loitering",
            "weapon": "Weapon Detected",
            "threat": "Threat Detected",
            "motion": "Motion Detected",
        }

        lower = raw_xml.lower()

        for key, value in keywords.items():
            if key in lower:
                return value

        return "Unknown Event"

    except Exception:
        return "Unknown Event"


def send_whatsapp_alert(message):
    if not all([WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TO]):
        logging.warning("WhatsApp credentials missing")
        return

    url = (
        f"https://graph.facebook.com/v18.0/"
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
        "text": {"body": message},
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=8,
        )

        if response.status_code in [200, 201]:
            logging.info("WhatsApp alert sent")
            print("[+] WhatsApp alert sent")
        else:
            logging.error(
                "WhatsApp failed: %s - %s",
                response.status_code,
                response.text,
            )

    except Exception as e:
        logging.error("WhatsApp error: %s", e)


def send_gmail_alert(subject, body, snapshot_path=None):
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_TO]):
        logging.warning("Gmail credentials missing")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        if snapshot_path and os.path.exists(snapshot_path):
            with open(snapshot_path, "rb") as f:
                img = MIMEImage(f.read())

            img.add_header(
                "Content-Disposition",
                "attachment",
                filename=os.path.basename(snapshot_path),
            )
            msg.attach(img)

        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()

        logging.info("Gmail alert sent")
        print("[+] Gmail alert sent")

    except Exception as e:
        logging.error("Gmail error: %s", e)


def process_event(raw_event):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    event_type = extract_event_type(raw_event)

    alert_message = (
        "🚨 NexusAI Security Alert\n\n"
        f"• Time: {now}\n"
        f"• Location: {LOCATION}\n"
        f"• Camera: {CAMERA_NUMBER}\n"
        f"• Event: {event_type}\n"
    )

    print(f"[!] {event_type} detected at {now}")

    logging.info(
        "Event: %s | Camera: %s | Location: %s",
        event_type,
        CAMERA_NUMBER,
        LOCATION,
    )

    snapshot_file = get_snapshot()

    send_whatsapp_alert(alert_message)

    send_gmail_alert(
        subject=f"NexusAI Alert - {event_type} - {CAMERA_NUMBER}",
        body=alert_message,
        snapshot_path=snapshot_file,
    )


def listen_to_alert_stream():
    if not all([CAM_IP, CAM_USER, CAM_PASS]):
        print("[!] Missing camera credentials")
        return

    auth = HTTPDigestAuth(CAM_USER, CAM_PASS)

    while True:
        try:
            print(f"[*] Connecting to Hikvision stream → {ISAPI_URL}")
            logging.info("Connecting to alert stream...")

            with requests.get(
                ISAPI_URL,
                auth=auth,
                stream=True,
                timeout=60,
            ) as response:

                if response.status_code == 200:
                    print("[+] Connected to event stream")
                    logging.info("Successfully connected to event stream")

                    for line in response.iter_lines():
                        if line:
                            decoded = line.decode(
                                "utf-8",
                                errors="ignore",
                            )

                            if any(
                                key in decoded.lower()
                                for key in [
                                    "eventtype",
                                    "intrusion",
                                    "linedetection",
                                    "regionentrance",
                                    "regionexiting",
                                    "loitering",
                                    "weapon",
                                    "threat",
                                    "motion",
                                ]
                            ):
                                process_event(decoded)

                else:
                    print(
                        f"[!] Stream returned status: "
                        f"{response.status_code}"
                    )
                    logging.warning(
                        "Stream status: %s",
                        response.status_code,
                    )

        except requests.exceptions.RequestException as e:
            print(
                f"[!] Connection lost: {e}. "
                "Reconnecting in 10 seconds..."
            )
            logging.error("Connection lost: %s", e)
            time.sleep(10)

        except Exception as e:
            print(
                f"[!] Unexpected error: {e}. "
                "Reconnecting in 10 seconds..."
            )
            logging.error("Unexpected error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    listen_to_alert_stream()
