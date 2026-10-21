"""Isolated API regression tests for secure POS exception ingestion.

These tests use a temporary SQLite database and FastAPI's in-process test client.
They do not contact staging/production services or modify a real database.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.src import transaction_correlation as correlation


INGEST_TOKEN = "test-pos-ingest-token-only"
ENDPOINT = "/api/retail/transaction-exceptions"
TOKEN_HEADER = "X-NexusAI-POS-Ingest-Token"


def valid_pos_event(**overrides):
    """Return a valid flat vendor payload using a timezone-aware ISO 8601 timestamp."""
    payload = {
        "site_id": "spar-site-001",
        "source_event_id": "pos-evt-0001",
        "till_id": "lane-04",
        "exception_type": "refund",
        "cashier_id": "cashier-12",
        "cashier_name": "Test Cashier",
        "transaction_value": "125.50",
        "currency": "ZAR",
        "occurred_at": "2026-10-19T14:30:00+02:00",
        "metadata": {"payment_method": "card"},
    }
    payload.update(overrides)
    return payload


def make_test_client(tmp_path, monkeypatch):
    """Install the route against a fresh, isolated SQLite database."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUSAI_POS_INGEST_TOKEN", INGEST_TOKEN)
    monkeypatch.setattr(correlation, "DB_PATH", tmp_path / "pos-ingestion-tests.sqlite3")

    correlation.init_schema()
    app = FastAPI()
    correlation.install_transaction_correlation(app)
    return TestClient(app)


def test_successful_pos_exception_ingestion(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)

    response = client.post(
        ENDPOINT,
        json=valid_pos_event(),
        headers={TOKEN_HEADER: INGEST_TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "event_id": "pos-evt-0001",
        "state": "pending_video_correlation",
    }


@pytest.mark.parametrize(
    "token",
    ["incorrect-test-token", None],
    ids=["invalid-token", "missing-token"],
)
def test_invalid_token_rejection(tmp_path, monkeypatch, token):
    client = make_test_client(tmp_path, monkeypatch)
    headers = {} if token is None else {TOKEN_HEADER: token}

    response = client.post(ENDPOINT, json=valid_pos_event(), headers=headers)

    assert response.status_code in (401, 403)


def test_duplicate_transaction_conflict(tmp_path, monkeypatch):
    client = make_test_client(tmp_path, monkeypatch)
    headers = {TOKEN_HEADER: INGEST_TOKEN}
    payload = valid_pos_event()

    first_response = client.post(ENDPOINT, json=payload, headers=headers)
    second_response = client.post(ENDPOINT, json=payload, headers=headers)

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["detail"] == "POS event already exists."
