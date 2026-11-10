import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import distributor_portal as distributors


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(distributors, "DB_PATH", tmp_path / "distributors.sqlite3")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUSAI_ADMIN_KEY", "test-admin-key")
    distributors.REGISTER_RATE.clear()
    distributors.LOGIN_RATE.clear()
    distributors.init_distributor_db()
    app = FastAPI()
    app.include_router(distributors.router)
    app.include_router(distributors.admin_router)
    return TestClient(app)


def register(client, email="channel@example.com"):
    return client.post("/api/distributors/register", json={
        "full_name": "Channel Manager",
        "company_name": "Example Distribution",
        "email": email,
        "country": "South Africa",
        "password": "a-secure-password-123",
    })


def admin_headers():
    return {"X-NexusAI-Admin-Key": "test-admin-key"}


def test_pending_distributor_requires_approval(client):
    response = register(client)
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
    login = client.post("/api/distributors/login", json={
        "email": "channel@example.com", "password": "a-secure-password-123"
    })
    assert login.status_code == 403


def test_distributor_referrals_are_isolated_and_rewards_require_conversion(client):
    register(client)
    accounts = client.get("/api/admin/distributors", headers=admin_headers()).json()["distributors"]
    distributor_id = accounts[0]["distributor_id"]
    approved = client.post(
        f"/api/admin/distributors/{distributor_id}/status",
        headers=admin_headers(),
        json={"status": "ACTIVE"},
    )
    assert approved.status_code == 200

    login = client.post("/api/distributors/login", json={
        "email": "channel@example.com", "password": "a-secure-password-123"
    })
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    referral = client.post("/api/distributors/referrals", headers=headers, json={
        "installer_company": "Regional CCTV Installers",
        "contact_name": "Installer Contact",
        "contact_email": "installer@example.com",
        "contact_phone": "",
        "country": "South Africa",
        "province": "KwaZulu-Natal",
        "relationship": "Existing distribution relationship",
        "notes": "",
        "confirms_permission": True,
    })
    assert referral.status_code == 200
    referral_id = referral.json()["referral_id"]

    dashboard = client.get("/api/distributors/me", headers=headers).json()
    assert len(dashboard["referrals"]) == 1
    assert dashboard["rewards"] == []

    converted = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(),
        json={"status": "CONVERTED", "verified_cameras": 6, "reward_amount_rands": 900},
    )
    assert converted.status_code == 200
    dashboard = client.get("/api/distributors/me", headers=headers).json()
    assert dashboard["summary"]["verified_cameras"] == 6
    assert dashboard["summary"]["rewards_pending_rands"] == 900.0

    reward_id = dashboard["rewards"][0]["reward_id"]
    paid = client.post(
        f"/api/admin/distributors/rewards/{reward_id}/pay",
        headers=admin_headers(),
        json={"payment_reference": "BANK-REF-001"},
    )
    assert paid.status_code == 200
    dashboard = client.get("/api/distributors/me", headers=headers).json()
    assert dashboard["summary"]["rewards_paid_rands"] == 900.0
    assert dashboard["summary"]["rewards_pending_rands"] == 0.0


def test_referral_requires_contact_permission_and_admin_key(client):
    register(client)
    rows = client.get("/api/admin/distributors", headers=admin_headers()).json()["distributors"]
    distributor_id = rows[0]["distributor_id"]
    client.post(f"/api/admin/distributors/{distributor_id}/status",
                headers=admin_headers(), json={"status": "ACTIVE"})
    login = client.post("/api/distributors/login", json={
        "email": "channel@example.com", "password": "a-secure-password-123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.post("/api/distributors/referrals", headers=headers, json={
        "installer_company": "No Consent Installers",
        "contact_name": "Contact",
        "contact_email": "no-consent@example.com",
        "country": "South Africa",
        "confirms_permission": False,
    })
    assert response.status_code == 400
    assert client.get("/api/admin/distributors").status_code == 401
