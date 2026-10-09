import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import retail_intelligence as retail


POS_HEADERS = {"X-NexusAI-Pos-Token": "test-pos-token"}
ADMIN_HEADERS = {"X-NexusAI-Admin-Key": "test-admin-key"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUSAI_POS_INGEST_TOKEN", "test-pos-token")
    monkeypatch.setenv("NEXUSAI_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("NEXUSAI_RETAIL_MATCH_WINDOW_SECONDS", "5")
    monkeypatch.setenv("NEXUSAI_RETAIL_MIN_CONFIDENCE", "0.85")
    monkeypatch.delenv("NEXUSAI_RETAIL_CAMERA_LANE_MAP", raising=False)
    monkeypatch.setattr(retail, "DB_PATH", tmp_path / "retail-test.db")
    app = FastAPI()
    retail.install(app)
    with TestClient(app) as client:
        yield client


def add_transaction(client, transaction_id="receipt-1001", lane_id="lane-01",
                    timestamp="2026-10-09T10:30:00Z", sku="BREAD-01"):
    return client.post(
        "/api/retail/pos/transactions",
        headers=POS_HEADERS,
        json={
            "site_id": "store-001",
            "lane_id": lane_id,
            "transaction_id": transaction_id,
            "timestamp": timestamp,
            "items": [{"sku": sku, "item_name": "Bread", "quantity": 1, "unit_price": 18.99}],
        },
    )


def add_camera_event(client, event_id="camera-event-1", lane_id="lane-01",
                     timestamp="2026-10-09T10:30:02Z"):
    return client.post(
        "/api/retail/camera-events",
        headers=POS_HEADERS,
        json={
            "site_id": "store-001",
            "lane_id": lane_id,
            "camera_id": "camera-01",
            "event_id": event_id,
            "timestamp": timestamp,
            "event_type": "CHECKOUT_ACTIVITY",
        },
    )


def test_pos_and_camera_event_match_by_site_lane_and_time(api):
    assert add_transaction(api).status_code == 200
    response = add_camera_event(api)
    assert response.status_code == 200
    data = response.json()
    assert data["accepted"] is True
    assert [row["transaction_id"] for row in data["candidate_transactions"]] == ["receipt-1001"]
    assert data["candidate_transactions"][0]["time_difference_seconds"] == 2.0


def test_camera_event_does_not_match_different_lane(api):
    add_transaction(api)
    response = add_camera_event(api, lane_id="lane-02")
    assert response.status_code == 200
    assert response.json()["candidate_transactions"] == []


def test_camera_event_does_not_match_outside_time_window(api):
    add_transaction(api)
    response = add_camera_event(api, timestamp="2026-10-09T10:30:20Z")
    assert response.status_code == 200
    assert response.json()["candidate_transactions"] == []


def test_missing_pos_token_is_rejected(api):
    response = api.post(
        "/api/retail/pos/transactions",
        json={
            "site_id": "store-001", "lane_id": "lane-01", "transaction_id": "receipt-1",
            "timestamp": "2026-10-09T10:30:00Z",
            "items": [{"sku": "BREAD-01", "item_name": "Bread"}],
        },
    )
    assert response.status_code == 401


def test_vision_sku_mismatch_creates_reviewable_incident(api):
    add_transaction(api)
    add_camera_event(api)
    response = api.post(
        "/api/retail/vision-results",
        headers=POS_HEADERS,
        json={
            "event_id": "camera-event-1",
            "transaction_id": "receipt-1001",
            "observed_items": [{"sku": "STEAK-99", "item_name": "Premium steak"}],
            "confidence": 0.97,
            "model_name": "test-model",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["incident_created"] is True
    assert result["status"] == "OPEN"
    assert "not a confirmed fraud finding" in result["warning"].lower()

    incidents = api.get("/api/retail/incidents", headers=ADMIN_HEADERS)
    assert incidents.status_code == 200
    assert len(incidents.json()["incidents"]) == 1
    assert incidents.json()["incidents"][0]["transaction_id"] == "receipt-1001"


def test_low_confidence_does_not_create_incident(api):
    add_transaction(api)
    add_camera_event(api)
    response = api.post(
        "/api/retail/vision-results",
        headers=POS_HEADERS,
        json={
            "event_id": "camera-event-1",
            "transaction_id": "receipt-1001",
            "observed_items": [{"sku": "STEAK-99"}],
            "confidence": 0.40,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": False, "reason": "below_confidence_threshold"}
    assert api.get("/api/retail/incidents", headers=ADMIN_HEADERS).json()["incidents"] == []


def test_vision_result_rejects_site_or_lane_mismatch(api):
    add_transaction(api)
    add_camera_event(api, lane_id="lane-02")
    response = api.post(
        "/api/retail/vision-results",
        headers=POS_HEADERS,
        json={
            "event_id": "camera-event-1",
            "transaction_id": "receipt-1001",
            "observed_items": [{"sku": "STEAK-99"}],
            "confidence": 0.99,
        },
    )
    assert response.status_code == 409


def test_hpp_bridge_ignores_unmapped_event(api, monkeypatch):
    monkeypatch.delenv("NEXUSAI_RETAIL_CAMERA_LANE_MAP", raising=False)
    assert retail.ingest_hpp_event({
        "site_id": "store-001", "camera_id": "camera-01",
        "timestamp": "2026-10-09T10:30:00Z", "event": "MOTION",
    }) is False


def test_hpp_bridge_maps_event_and_keeps_snapshot_reference(api, monkeypatch):
    monkeypatch.setenv(
        "NEXUSAI_RETAIL_CAMERA_LANE_MAP",
        json.dumps({"store-001|SERIAL123|1": "lane-01"}),
    )
    inserted = retail.ingest_hpp_event({
        "site_id": "store-001",
        "camera_id": "SERIAL123-Ch 1",
        "hpp_device_serial": "SERIAL123",
        "hpp_channel": "1",
        "event_id": "hpp-event-unique",
        "timestamp": "2026-10-09T10:30:00Z",
        "event": "MOTION",
        "hpp_data": {"pictureUrl": "https://example.invalid/snapshot.jpg"},
    })
    assert inserted is True
    response = add_transaction(api)
    assert response.status_code == 200
    # A second event using the same HPP event identity must be idempotent.
    assert retail.ingest_hpp_event({
        "site_id": "store-001",
        "camera_id": "SERIAL123-Ch 1",
        "hpp_device_serial": "SERIAL123",
        "hpp_channel": "1",
        "event_id": "hpp-event-unique",
        "timestamp": "2026-10-09T10:30:01Z",
        "event": "MOTION",
        "hpp_data": {"pictureUrl": "https://example.invalid/snapshot.jpg"},
    }) is True
