"""Interactive one-time NexusAI Security Box setup."""
from __future__ import annotations
import argparse
import getpass
import os
from edge_agent.security_box import bootstrap, save_site_config
from edge_agent import secure_store

parser = argparse.ArgumentParser()
parser.add_argument("--installer-token", required=True)
parser.add_argument("--api-url", default="https://getnexusai.co.za")
args = parser.parse_args()

data = bootstrap(args.installer_token, args.api_url)
site_id = str(data["site_id"])
print("NexusAI site connected:", site_id)
save_site_config(site_id, args.api_url)

username = input("Hikvision NVR username: ").strip()
password = getpass.getpass("Hikvision NVR password: ")
if not username or not password:
    raise SystemExit("A Hikvision username and password are required.")

secure_store.set_hikvision_credentials(site_id, username, password)
print("Hikvision credentials saved in the operating system secure store.")
print("NexusAI Security Box provisioning complete.")
print("The background service can now start automatically.")
