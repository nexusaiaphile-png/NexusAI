from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import hpp_integration as hpp


def test_hpp_site_connect_discovers_maps_and_subscribes_devices(monkeypatch):
    monkeypatch.setenv("NEXUSAI_ADMIN_KEY", "test-admin")
    monkeypatch.setattr(hpp, "configured", lambda: True)
    monkeypatch.setattr(
        hpp, "search_sites",
        lambda page=1, page_size=50, search="": {
            "errorCode": "0",
            "data": {"rows": [{"siteId": "hpp-1", "siteName": "Retail Store"}]},
        },
    )
    monkeypatch.setattr(
        hpp, "list_devices",
        lambda site_id="", page=1, page_size=100, device_serial="": {
            "errorCode": "0",
            "data": {"rows": [{"deviceSerial": "CAM-001"}, {"deviceSerial": "NVR-002"}]},
        },
    )
    monkeypatch.setattr(
        hpp, "list_cameras",
        lambda serial: {"errorCode": "0", "data": {"cameras": [{"channel": serial + "-CH1"}]}},
    )
    subscribed = []
    monkeypatch.setattr(hpp, "subscribe", lambda req: subscribed.extend(req.device_serial_list) or {"success": True})
    onboarded = []
    app = FastAPI()
    hpp.install(
        app,
        event_sink=lambda event: True,
        site_onboarder=lambda *args: onboarded.append(args),
    )

    response = TestClient(app).post(
        "/api/hpp/sites/connect",
        headers={"X-NexusAI-Admin-Key": "test-admin"},
        json={"hpp_site_id": "hpp-1", "nexusai_site_id": "site-123456"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["devices_discovered"] == 2
    assert body["cameras_or_channels_discovered"] == 2
    assert body["subscribed"] is True
    assert subscribed == ["CAM-001", "NVR-002"]
    assert onboarded[0][0:2] == ("hpp-1", "site-123456")


def test_hpp_site_connect_rejects_sites_not_authorised_in_hpp(monkeypatch):
    monkeypatch.setenv("NEXUSAI_ADMIN_KEY", "test-admin")
    monkeypatch.setattr(hpp, "configured", lambda: True)
    monkeypatch.setattr(hpp, "search_sites", lambda **kwargs: {"errorCode": "0", "data": {"rows": []}})
    app = FastAPI()
    hpp.install(app, site_onboarder=lambda *args: None)

    response = TestClient(app).post(
        "/api/hpp/sites/connect",
        headers={"X-NexusAI-Admin-Key": "test-admin"},
        json={"hpp_site_id": "not-authorised", "nexusai_site_id": "site-123456"},
    )

    assert response.status_code == 404
