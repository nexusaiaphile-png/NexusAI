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
    migration = open("backend/migrations/20261115_retail_notification_routing.sql", encoding="utf-8").read()
    # Execute equivalent SQLite-compatible DDL for the migration tables.
    conn.executescript("""
      CREATE TABLE nexusai_retail_organisations(organisation_id TEXT PRIMARY KEY,organisation_name TEXT NOT NULL,active BOOLEAN NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL);
      CREATE TABLE nexusai_retail_organisation_sites(organisation_id TEXT NOT NULL,site_id TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,PRIMARY KEY(organisation_id,site_id));
      CREATE TABLE nexusai_retail_notification_recipients(recipient_id TEXT PRIMARY KEY,organisation_id TEXT NOT NULL,role TEXT NOT NULL,display_name TEXT NOT NULL,email TEXT NOT NULL,phone_e164 TEXT,active BOOLEAN NOT NULL DEFAULT 1,dashboard_opt_in BOOLEAN NOT NULL DEFAULT 1,email_opt_in BOOLEAN NOT NULL DEFAULT 0,whatsapp_opt_in BOOLEAN NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL);
      CREATE TABLE nexusai_retail_recipient_sites(recipient_id TEXT NOT NULL,organisation_id TEXT NOT NULL,site_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(recipient_id,site_id));
      CREATE TABLE nexusai_retail_notification_tokens(token_hash TEXT PRIMARY KEY,recipient_id TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,revoked_at REAL);
      CREATE TABLE nexusai_retail_notifications(notification_id TEXT PRIMARY KEY,organisation_id TEXT NOT NULL,site_id TEXT NOT NULL,pos_exception_id INTEGER NOT NULL,source_event_id TEXT NOT NULL,exception_type TEXT NOT NULL,occurred_at TEXT NOT NULL,summary TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(site_id,source_event_id));
      CREATE TABLE nexusai_retail_notification_deliveries(delivery_id TEXT PRIMARY KEY,notification_id TEXT NOT NULL,recipient_id TEXT NOT NULL,channel TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',attempt_count INTEGER NOT NULL DEFAULT 0,provider_reference TEXT,error_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(notification_id,recipient_id,channel));
    """)
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(notifications.router)
    client = TestClient(app)
    login = client.post("/api/admin/distributors/auth-login", json={"pin":"test-admin-pin-123"}) if False else None
    from backend.src.distributor_portal import admin_router
    app.include_router(admin_router)
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
        "role":"STORE_MANAGER","display_name":"Store Manager","email":"manager@example.com",
        "site_ids":["site-branch0001"]})
    assert manager.status_code == 200
    head = client.post("/api/admin/retail/organisations/retailer-one/recipients", headers=client.admin, json={
        "role":"HEAD_OFFICE","display_name":"Head Office","email":"office@example.com",
        "site_ids":["site-branch0001","site-branch0002"]})
    assert head.status_code == 200
    with sqlite3.connect(tc.DB_PATH) as conn:
        for site, event_id, event_type in [
            ("site-branch0001","event-001","refund"),("site-branch0002","event-002","void")]:
            cur=conn.execute("""INSERT INTO nexusai_pos_exceptions(site_id,till_id,exception_type,occurred_at,created_at,source_event_id)
                VALUES(?,?,?,?,?,?)""",(site,"till-1",event_type,"2026-11-15T10:00:00Z",time.time(),event_id))
            exception_id=cur.lastrowid
            notifications.create_for_pos_exception(site,exception_id,event_id,True)
    manager_headers={"Authorization":"Bearer "+manager.json()["access_token"]}
    head_headers={"Authorization":"Bearer "+head.json()["access_token"]}
    manager_rows=client.get("/api/retail/notifications",headers=manager_headers).json()["notifications"]
    head_rows=client.get("/api/retail/notifications",headers=head_headers).json()["notifications"]
    assert len(manager_rows)==1 and manager_rows[0]["site_id"]=="site-branch0001"
    assert len(head_rows)==2
    assert client.get("/api/retail/notifications",headers={"Authorization":"Bearer "+manager.json()["access_token"]}).status_code==200


def test_manager_cannot_be_assigned_multiple_branches_and_invalid_token_is_denied(client):
    client.post("/api/admin/retail/organisations", headers=client.admin, json={
        "organisation_id":"retailer-two","organisation_name":"Retailer Two",
        "site_ids":["site-branch0001","site-branch0002"]})
    response=client.post("/api/admin/retail/organisations/retailer-two/recipients",headers=client.admin,json={
        "role":"STORE_MANAGER","display_name":"Manager","email":"manager2@example.com",
        "site_ids":["site-branch0001","site-branch0002"]})
    assert response.status_code==422
    assert client.get("/api/retail/notifications",headers={"Authorization":"Bearer "+"x"*40}).status_code==401
