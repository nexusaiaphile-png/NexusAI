import hashlib
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src import retail_notifications as notifications
from backend.src import transaction_correlation as tc


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "retail.sqlite3"
    monkeypatch.setattr(tc, "DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUSAI_ADMIN_PIN", "test-admin-pin-123")
    tc.init_schema()
    conn = sqlite3.connect(db_path)
    conn.executescript("""
      CREATE TABLE nexusai_customers (
        site_id TEXT PRIMARY KEY, business_name TEXT NOT NULL, store_address TEXT,
        contact_name TEXT, contact_phone TEXT, contact_email TEXT, camera_count INTEGER NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE');
      INSERT INTO nexusai_customers(site_id,business_name,created_at) VALUES
        ('site-branch0001','Durban Central',1),('site-branch0002','Pietermaritzburg',1);
    """)
    # Execute equivalent SQLite-compatible DDL for the migration tables.
    conn.executescript("""
      CREATE TABLE nexusai_retail_organisations(organisation_id TEXT PRIMARY KEY,organisation_name TEXT NOT NULL,active BOOLEAN NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL);
      CREATE TABLE nexusai_retail_organisation_sites(organisation_id TEXT NOT NULL,site_id TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,PRIMARY KEY(organisation_id,site_id));
      CREATE TABLE nexusai_retail_notification_recipients(recipient_id TEXT PRIMARY KEY,organisation_id TEXT NOT NULL,role TEXT NOT NULL,display_name TEXT NOT NULL,active BOOLEAN NOT NULL DEFAULT 1,dashboard_opt_in BOOLEAN NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL);
      CREATE TABLE nexusai_retail_recipient_sites(recipient_id TEXT NOT NULL,organisation_id TEXT NOT NULL,site_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(recipient_id,site_id));
      CREATE TABLE nexusai_retail_notification_tokens(token_hash TEXT PRIMARY KEY,recipient_id TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,revoked_at REAL);
      CREATE TABLE nexusai_retail_notifications(notification_id TEXT PRIMARY KEY,organisation_id TEXT NOT NULL,site_id TEXT NOT NULL,pos_exception_id INTEGER NOT NULL,source_event_id TEXT NOT NULL,exception_type TEXT NOT NULL,occurred_at TEXT NOT NULL,summary TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(site_id,source_event_id));
      CREATE TABLE nexusai_retail_notification_deliveries(delivery_id TEXT PRIMARY KEY,notification_id TEXT NOT NULL,recipient_id TEXT NOT NULL,channel TEXT NOT NULL CHECK(channel='DASHBOARD'),status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','QUEUED','READ','FAILED')),attempt_count INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(notification_id,recipient_id,channel));
      CREATE TABLE nexusai_retail_push_subscriptions(subscription_id TEXT PRIMARY KEY,recipient_id TEXT NOT NULL,endpoint TEXT NOT NULL UNIQUE,subscription_json TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,revoked_at REAL);
    """)
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(notifications.router)
    from backend.src.distributor_portal import admin_router
    app.include_router(admin_router)
    client = TestClient(app)
    login = client.post("/api/admin/distributors/auth-login", json={"pin":"test-admin-pin-123"})
    assert login.status_code == 200
    client.admin = {"Authorization":"Bearer "+login.json()["access_token"]}
    return client


def test_manager_sees_only_assigned_branch_and_head_office_sees_assigned_branches(client):
    org = client.post("/api/admin/retail/organisations", headers=client.admin, json={
        "organisation_id":"retailer-one","organisation_name":"Retailer One",
        "site_ids":["site-branch0001","site-branch0002"]})
    assert org.status_code == 200
    manager = client.post("/api/admin/retail/organisations/retailer-one/recipients", headers=client.admin, json={
        "role":"STORE_MANAGER","display_name":"Store Manager",
        "site_ids":["site-branch0001"]})
    assert manager.status_code == 200
    head = client.post("/api/admin/retail/organisations/retailer-one/recipients", headers=client.admin, json={
        "role":"HEAD_OFFICE","display_name":"Head Office",
        "site_ids":["site-branch0001","site-branch0002"]})
    assert head.status_code == 200
    inserted = []
    with sqlite3.connect(tc.DB_PATH) as conn:
        for site, event_id, event_type in [
            ("site-branch0001","event-001","refund"),("site-branch0002","event-002","void")]:
            cur=conn.execute("""INSERT INTO nexusai_pos_exceptions(site_id,till_id,exception_type,occurred_at,created_at,source_event_id)
                VALUES(?,?,?,?,?,?)""",(site,"till-1",event_type,"2026-11-15T10:00:00Z",time.time(),event_id))
            inserted.append((site,cur.lastrowid,event_id))
    # The POS insert must commit before the independent post-commit notification task runs.
    for site, exception_id, event_id in inserted:
        notifications.create_for_pos_exception(site,exception_id,event_id,True)
    manager_headers={"Authorization":"Bearer "+manager.json()["access_token"]}
    head_headers={"Authorization":"Bearer "+head.json()["access_token"]}
    manager_rows=client.get("/api/retail/notifications",headers=manager_headers).json()["notifications"]
    head_rows=client.get("/api/retail/notifications",headers=head_headers).json()["notifications"]
    assert len(manager_rows)==1 and manager_rows[0]["site_id"]=="site-branch0001"
    assert len(head_rows)==2
    notification_id=manager_rows[0]["notification_id"]
    marked=client.post("/api/retail/notifications/"+notification_id+"/read",headers=manager_headers)
    assert marked.status_code==200 and marked.json()["status"]=="READ"
    assert client.get("/api/retail/notifications",headers={"Authorization":"Bearer "+manager.json()["access_token"]}).status_code==200


def test_manager_cannot_be_assigned_multiple_branches_and_invalid_token_is_denied(client):
    client.post("/api/admin/retail/organisations", headers=client.admin, json={
        "organisation_id":"retailer-two","organisation_name":"Retailer Two",
        "site_ids":["site-branch0001","site-branch0002"]})
    response=client.post("/api/admin/retail/organisations/retailer-two/recipients",headers=client.admin,json={
        "role":"STORE_MANAGER","display_name":"Manager",
        "site_ids":["site-branch0001","site-branch0002"]})
    assert response.status_code==422
    assert client.get("/api/retail/notifications",headers={"Authorization":"Bearer "+"x"*40}).status_code==401


def test_push_subscription_requires_authenticated_recipient_and_valid_subscription(client):
    assert client.get("/api/retail/push/config").status_code == 200
    assert client.post("/api/retail/push/subscribe",json={"subscription":{}}).status_code == 401


def test_delivery_schema_rejects_external_channels(client):
    with sqlite3.connect(tc.DB_PATH) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO nexusai_retail_notification_deliveries(delivery_id,notification_id,recipient_id,channel,created_at,updated_at) VALUES ('d','n','r','WHATSAPP',1,1)")


def test_recipient_can_register_and_revoke_own_browser_push_subscription(client):
    client.post("/api/admin/retail/organisations", headers=client.admin, json={
        "organisation_id":"retailer-push","organisation_name":"Retailer Push",
        "site_ids":["site-branch0001"]})
    created=client.post("/api/admin/retail/organisations/retailer-push/recipients",headers=client.admin,json={
        "role":"STORE_MANAGER","display_name":"Push Manager","site_ids":["site-branch0001"]})
    headers={"Authorization":"Bearer "+created.json()["access_token"]}
    subscription={"endpoint":"https://push.example.test/subscription/12345678901234567890",
        "keys":{"p256dh":"public-key","auth":"auth-key"}}
    registered=client.post("/api/retail/push/subscribe",headers=headers,json={"subscription":subscription})
    assert registered.status_code==200 and registered.json()["registered"] is True
    with sqlite3.connect(tc.DB_PATH) as conn:
        row=conn.execute("SELECT recipient_id,revoked_at FROM nexusai_retail_push_subscriptions WHERE endpoint=?",
                         (subscription["endpoint"],)).fetchone()
    assert row[0]==created.json()["recipient_id"] and row[1] is None
    revoked=client.request("DELETE","/api/retail/push/subscribe",headers=headers,json={"endpoint":subscription["endpoint"]})
    assert revoked.status_code==200
    with sqlite3.connect(tc.DB_PATH) as conn:
        row=conn.execute("SELECT revoked_at FROM nexusai_retail_push_subscriptions WHERE endpoint=?",
                         (subscription["endpoint"],)).fetchone()
    assert row[0] is not None
