"""Security and edge-case tests for Retail Intelligence correlation.

Every test uses a disposable SQLite database; no production or developer DB is touched.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import retail_intelligence as retail


@pytest.fixture
def retail_client(tmp_path, monkeypatch):
    monkeypatch.setattr(retail, "DB_PATH", tmp_path / "retail-edge-tests.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEXUSAI_ADMIN_KEY", raising=False)
    monkeypatch.setenv("NEXUSAI_POS_INGEST_TOKEN", "test-pos-token")
    monkeypatch.setenv("NEXUSAI_RETAIL_MIN_CONFIDENCE", "0.85")
    app = FastAPI()
    retail.install(app)
    with TestClient(app) as client:
        yield client, tmp_path / "retail-edge-tests.db"


def payloads(*, site_id="test-site-001", lane_id="lane-01", tx_id="tx-edge-test",
             event_id="event-edge-test", timestamp=None, sku="SKU-001"):
    stamp = timestamp or datetime.now(timezone.utc)
    iso = stamp.isoformat()
    return (
        {
            "site_id": site_id,
            "lane_id": lane_id,
            "transaction_id": tx_id,
            "timestamp": iso,
            "items": [{"sku": sku, "item_name": "Test item", "quantity": 1, "unit_price": 10.0}],
        },
        {
            "site_id": site_id,
            "lane_id": lane_id,
            "camera_id": "camera-01",
            "event_id": event_id,
            "timestamp": iso,
        },
    )


def ingest_pos_and_camera(client, *, site_id="test-site-001", lane_id="lane-01",
                          tx_id="tx-edge-test", event_id="event-edge-test",
                          timestamp=None, sku="SKU-001"):
    pos_payload, camera_payload = payloads(
        site_id=site_id, lane_id=lane_id, tx_id=tx_id, event_id=event_id,
        timestamp=timestamp, sku=sku,
    )
    headers = {"X-NexusAI-POS-Token": "test-pos-token"}
    pos = client.post("/api/retail/pos/transactions", headers=headers, json=pos_payload)
    camera = client.post("/api/retail/camera-events", headers=headers, json=camera_payload)
    return pos, camera, headers


@pytest.mark.parametrize("headers", [{}, {"X-NexusAI-POS-Token": "wrong-token"}], ids=["missing-token", "invalid-token"])
def test_pos_ingestion_rejects_missing_or_invalid_token(retail_client, headers):
    client, _ = retail_client
    pos_payload, _ = payloads()
    response = client.post("/api/retail/pos/transactions", headers=headers, json=pos_payload)
    assert response.status_code in (401, 403), response.text


def test_matching_sku_does_not_create_discrepancy_incident(retail_client):
    client, db_path = retail_client
    pos, camera, headers = ingest_pos_and_camera(client)
    assert pos.status_code == 200, pos.text
    assert camera.status_code == 200, camera.text
    vision = client.post(
        "/api/retail/vision-results",
        headers=headers,
        json={
            "event_id": "event-edge-test",
            "transaction_id": "tx-edge-test",
            "observed_items": [{"sku": "sku-001"}],
            "confidence": 0.99,
            "model_name": "synthetic-test",
        },
    )
    assert vision.status_code == 200, vision.text
    assert vision.json()["incident_created"] is False
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM retail_incidents").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("camera_site", "camera_lane"),
    [("different-site", "lane-01"), ("test-site-001", "lane-99")],
    ids=["site-mismatch", "lane-mismatch"],
)
def test_vision_rejects_site_or_lane_mismatch(retail_client, camera_site, camera_lane):
    client, _ = retail_client
    headers = {"X-NexusAI-POS-Token": "test-pos-token"}
    pos_payload, _ = payloads()
    _, camera_payload = payloads(site_id=camera_site, lane_id=camera_lane)
    assert client.post("/api/retail/pos/transactions", headers=headers, json=pos_payload).status_code == 200
    assert client.post("/api/retail/camera-events", headers=headers, json=camera_payload).status_code == 200
    response = client.post(
        "/api/retail/vision-results",
        headers=headers,
        json={
            "event_id": "event-edge-test",
            "transaction_id": "tx-edge-test",
            "observed_items": [{"sku": "OTHER-SKU"}],
            "confidence": 0.99,
            "model_name": "synthetic-test",
        },
    )
    assert response.status_code == 409, response.text


def test_vision_rejects_event_outside_time_window(retail_client):
    client, _ = retail_client
    old_stamp = datetime.now(timezone.utc) - timedelta(seconds=retail.WINDOW + 30)
    _, _, headers = ingest_pos_and_camera(client, timestamp=old_stamp)
    # Both records share the same old timestamp; make the camera event's timestamp
    # current so the event/transaction pair is outside the allowed correlation window.
    camera_payload = {
        "site_id": "test-site-001",
        "lane_id": "lane-01",
        "camera_id": "camera-01",
        "event_id": "event-current-time",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    assert client.post("/api/retail/camera-events", headers=headers, json=camera_payload).status_code == 200
    response = client.post(
        "/api/retail/vision-results",
        headers=headers,
        json={
            "event_id": "event-current-time",
            "transaction_id": "tx-edge-test",
            "observed_items": [{"sku": "OTHER-SKU"}],
            "confidence": 0.99,
            "model_name": "synthetic-test",
        },
    )
    assert response.status_code == 422, response.text


def test_duplicate_pos_and_camera_submissions_do_not_duplicate_records(retail_client):
    client, db_path = retail_client
    pos, camera, _ = ingest_pos_and_camera(client)
    assert pos.status_code == 200, pos.text
    assert camera.status_code == 200, camera.text
    pos_payload, camera_payload = payloads()
    headers = {"X-NexusAI-POS-Token": "test-pos-token"}
    assert client.post("/api/retail/pos/transactions", headers=headers, json=pos_payload).status_code == 200
    assert client.post("/api/retail/camera-events", headers=headers, json=camera_payload).status_code == 200
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM retail_pos_transactions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM retail_camera_events").fetchone()[0] == 1


def test_duplicate_vision_submission_does_not_duplicate_incident(retail_client):
    client, db_path = retail_client
    _, _, headers = ingest_pos_and_camera(client, sku="RECEIPT-SKU")
    vision_payload = {
        "event_id": "event-edge-test",
        "transaction_id": "tx-edge-test",
        "observed_items": [{"sku": "OBSERVED-SKU"}],
        "confidence": 0.99,
        "model_name": "synthetic-test",
    }
    first = client.post("/api/retail/vision-results", headers=headers, json=vision_payload)
    second = client.post("/api/retail/vision-results", headers=headers, json=vision_payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM retail_vision_results").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM retail_incidents").fetchone()[0] == 1


def test_below_confidence_threshold_is_not_accepted_and_creates_no_incident(retail_client):
    client, db_path = retail_client
    _, _, headers = ingest_pos_and_camera(client, sku="RECEIPT-SKU")
    response = client.post(
        "/api/retail/vision-results",
        headers=headers,
        json={
            "event_id": "event-edge-test",
            "transaction_id": "tx-edge-test",
            "observed_items": [{"sku": "OBSERVED-SKU"}],
            "confidence": 0.84,
            "model_name": "synthetic-test",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": False, "reason": "below_confidence_threshold"}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM retail_vision_results").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM retail_incidents").fetchone()[0] == 0


def test_confidence_at_threshold_is_processed(retail_client):
    client, db_path = retail_client
    _, _, headers = ingest_pos_and_camera(client, sku="RECEIPT-SKU")
    response = client.post(
        "/api/retail/vision-results",
        headers=headers,
        json={
            "event_id": "event-edge-test",
            "transaction_id": "tx-edge-test",
            "observed_items": [{"sku": "OBSERVED-SKU"}],
            "confidence": 0.85,
            "model_name": "synthetic-test",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["incident_created"] is True
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM retail_incidents").fetchone()[0] == 1
