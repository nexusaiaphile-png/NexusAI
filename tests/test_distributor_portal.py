import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import distributor_portal as distributors


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(distributors, "DB_PATH", tmp_path / "distributors.sqlite3")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-google-client-id")
    monkeypatch.setattr(distributors, "_verify_google_identity_token", lambda credential, client_id: {"email": "nexusai.aphile@gmail.com", "email_verified": True})
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
    return {"Authorization": "Bearer test-google-id-token"}


def test_pending_distributor_requires_approval(client):
    response = register(client)
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
    login = client.post("/api/distributors/login", json={
        "email": "channel@example.com", "password": "a-secure-password-123"
    })
    assert login.status_code == 403


def test_distributor_referrals_are_isolated_and_conversion_does_not_create_one_off_rewards(client):
    register(client)
    accounts = client.get("/api/admin/distributors", headers=admin_headers()).json()["distributors"]
    distributor_id = accounts[0]["distributor_id"]
    blocked = client.post(
        f"/api/admin/distributors/{distributor_id}/status",
        headers=admin_headers(),
        json={"status": "ACTIVE"},
    )
    assert blocked.status_code == 400
    blank_agreement = client.post(
        f"/api/admin/distributors/{distributor_id}/status",
        headers=admin_headers(),
        json={"status": "ACTIVE", "agreement_version": "  ",
              "agreement_reference": "  "},
    )
    assert blank_agreement.status_code == 400
    approved = client.post(
        f"/api/admin/distributors/{distributor_id}/status",
        headers=admin_headers(),
        json={"status": "ACTIVE", "agreement_version": "2026-11-v1",
              "agreement_reference": "SIGNED-AGREEMENT-001"},
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

    duplicate = client.post("/api/distributors/referrals", headers=headers, json={
        "installer_company": "Duplicate Installer Record",
        "contact_name": "Installer Contact",
        "contact_email": "installer@example.com",
        "country": "South Africa",
        "confirms_permission": True,
    })
    assert duplicate.status_code == 409

    dashboard = client.get("/api/distributors/me", headers=headers).json()
    assert len(dashboard["referrals"]) == 1
    assert dashboard["rewards"] == []

    register(client, email="second-channel@example.com")
    second_rows = client.get("/api/admin/distributors", headers=admin_headers()).json()["distributors"]
    second_id = next(row["distributor_id"] for row in second_rows if row["email"] == "second-channel@example.com")
    second_approval = client.post(
        f"/api/admin/distributors/{second_id}/status",
        headers=admin_headers(),
        json={"status": "ACTIVE", "agreement_version": "2026-11-v1",
              "agreement_reference": "SIGNED-AGREEMENT-SECOND"},
    )
    assert second_approval.status_code == 200
    second_login = client.post("/api/distributors/login", json={
        "email": "second-channel@example.com", "password": "a-secure-password-123"
    })
    second_headers = {"Authorization": f"Bearer {second_login.json()['access_token']}"}
    second_dashboard = client.get("/api/distributors/me", headers=second_headers).json()
    assert second_dashboard["referrals"] == []
    assert second_dashboard["rewards"] == []

    premature = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(),
        json={"status": "CONVERTED", "verified_cameras": 6},
    )
    assert premature.status_code == 409
    contacted = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(), json={"status": "CONTACTED"},
    )
    assert contacted.status_code == 200
    qualified = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(), json={"status": "QUALIFIED"},
    )
    assert qualified.status_code == 200
    converted = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(),
        json={"status": "CONVERTED", "verified_cameras": 6, "reward_amount_rands": 900},
    )
    assert converted.status_code == 200
    closed_review = client.post(
        f"/api/admin/distributors/referrals/{referral_id}/review",
        headers=admin_headers(),
        json={"status": "CONVERTED", "verified_cameras": 6, "reward_amount_rands": 900},
    )
    assert closed_review.status_code == 409
    dashboard = client.get("/api/distributors/me", headers=headers).json()
    assert dashboard["summary"]["verified_cameras"] == 6
    # Camera count is for tracking only. Monthly commissions are reconciled
    # separately against active cameras and subscription payments received.
    assert dashboard["rewards"] == []
    assert dashboard["summary"]["rewards_pending_rands"] == 0.0
    assert dashboard["summary"]["rewards_paid_rands"] == 0.0


def test_referral_requires_contact_permission_and_admin_key(client):
    register(client)
    rows = client.get("/api/admin/distributors", headers=admin_headers()).json()["distributors"]
    distributor_id = rows[0]["distributor_id"]
    approved = client.post(f"/api/admin/distributors/{distributor_id}/status",
                headers=admin_headers(), json={"status": "ACTIVE", "agreement_version": "2026-11-v1",
                                                "agreement_reference": "SIGNED-AGREEMENT-002"})
    assert approved.status_code == 200
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


def test_distributor_admin_requires_google_owner_identity(client, monkeypatch):
    assert client.get("/api/admin/distributors").status_code == 401
    monkeypatch.setattr(distributors, "_verify_google_identity_token", lambda credential, client_id: {"email": "other@example.com", "email_verified": True})
    denied = client.get("/api/admin/distributors", headers={"Authorization": "Bearer other-google-token"})
    assert denied.status_code == 403
    monkeypatch.setattr(distributors, "_verify_google_identity_token", lambda credential, client_id: {"email": "nexusai.aphile@gmail.com", "email_verified": False})
    unverified = client.get("/api/admin/distributors", headers={"Authorization": "Bearer unverified-google-token"})
    assert unverified.status_code == 403


def test_admin_auth_config_exposes_only_public_client_configuration(client):
    response = client.get("/api/admin/distributors/auth-config")
    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "client_id": "test-google-client-id",
        "administrator_email": "nexusai.aphile@gmail.com",
    }
