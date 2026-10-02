"""NexusAI Security Box entry point.

This wrapper runs the existing hardened NexusAI Edge Agent under a platform
service manager. The service manager handles startup and crash recovery.
"""
import os
import requests
import edge_agent.agent as agent

def provision_from_environment():
    site_id = os.getenv("NEXUSAI_SITE_ID", "").strip()
    activation_code = os.getenv("NEXUSAI_ACTIVATION_CODE", "").strip()
    api_url = os.getenv("NEXUSAI_API_URL", "https://getnexusai.co.za").rstrip("/")
    if not site_id or not activation_code:
        return False
    response = requests.get(
        f"{api_url}/api/edge/provision",
        params={"site_id": site_id, "activation_code": activation_code},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    os.environ["NEXUSAI_EDGE_TOKEN"] = str(payload["edge_token"])
    os.environ["NEXUSAI_API_URL"] = str(payload.get("api_url") or api_url)
    return True

def run():
    provision_from_environment()
    return agent.main()

if __name__ == "__main__":
    run()
