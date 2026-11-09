import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from backend.src import partner_portal as portal


class PartnerPortalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = portal.DB_PATH
        self.old_database_url = os.environ.pop("DATABASE_URL", None)
        portal.DB_PATH = Path(self.temp_dir.name) / "partner-test.sqlite3"
        portal.RATE.clear()
        portal.init_partner_db()
        now = 1_800_000_000.0
        with portal._db() as conn:
            portal._sql(conn, """INSERT INTO nexusai_partner_accounts
                (partner_id,email,full_name,company_name,password_hash,status,created_at,activated_at)
                VALUES(%s,%s,%s,%s,%s,'ACTIVE',%s,%s)""",
                ("partner_one", "one@example.co.za", "Partner One", "Company One", "unused", now, now))
            portal._sql(conn, """INSERT INTO nexusai_partner_accounts
                (partner_id,email,full_name,company_name,password_hash,status,created_at,activated_at)
                VALUES(%s,%s,%s,%s,%s,'ACTIVE',%s,%s)""",
                ("partner_two", "two@example.co.za", "Partner Two", "Company Two", "unused", now, now))
            portal._sql(conn, """INSERT INTO nexusai_partner_deployments
                (deployment_id,partner_id,site_reference,business_name,camera_count,status,commission_rate,created_at)
                VALUES(%s,%s,%s,%s,%s,'VERIFIED',100,%s)""",
                ("dep_existing", "partner_two", "SITE-TWO", "Retail Two", 8, now))
            self.token_one = portal._issue_session(conn, "partner_one")
            self.token_two = portal._issue_session(conn, "partner_two")

    def tearDown(self):
        portal.DB_PATH = self.old_db_path
        if self.old_database_url is not None:
            os.environ["DATABASE_URL"] = self.old_database_url
        self.temp_dir.cleanup()

    def test_partner_dashboard_only_returns_own_deployments(self):
        dashboard = asyncio.run(portal.partner_dashboard("Bearer " + self.token_one))
        self.assertEqual(dashboard["partner"]["partner_id"], "partner_one")
        self.assertEqual(dashboard["deployments"], [])
        self.assertEqual(dashboard["summary"]["verified_cameras"], 0)

        other = asyncio.run(portal.partner_dashboard("Bearer " + self.token_two))
        self.assertEqual(len(other["deployments"]), 1)
        self.assertEqual(other["partner"]["partner_id"], "partner_two")

    def test_partner_submission_is_attributed_to_authenticated_account(self):
        payload = portal.AddDeployment(
            site_reference="SITE-ONE",
            business_name="Retail One",
            camera_count=4,
            notes="Authorised installation",
        )
        result = asyncio.run(portal.partner_submit_deployment(payload, "Bearer " + self.token_one))
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "PENDING")
        with portal._db() as conn:
            row = portal._sql(conn, """SELECT partner_id,status,camera_count
                FROM nexusai_partner_deployments WHERE deployment_id=%s""",
                (result["deployment_id"],)).fetchone()
        self.assertEqual(tuple(row), ("partner_one", "PENDING", 4))

    def test_self_registration_requires_admin_approval(self):
        os.environ["NEXUSAI_ADMIN_KEY"] = "test-admin-key"
        portal.REGISTER_RATE.clear()
        Request = __import__("starlette.requests", fromlist=["Request"]).Request
        scope = {"type":"http", "method":"POST", "path":"/", "headers":[],
                 "query_string":b"", "server":("test",80), "client":("127.0.0.9",1234), "scheme":"http"}
        request = Request(scope)
        payload = portal.RegisterPartner(email="newpartner@example.co.za", full_name="New Partner",
            company_name="New Company", password="a-strong-password-123")
        result = asyncio.run(portal.partner_register(payload, request))
        self.assertEqual(result["status"], "PENDING")
        with portal._db() as conn:
            row = portal._sql(conn, "SELECT partner_id,password_hash,status FROM nexusai_partner_accounts WHERE lower(email)=%s",
                              ("newpartner@example.co.za",)).fetchone()
        self.assertEqual(row[2], "PENDING")
        self.assertTrue(row[1].startswith("pbkdf2_sha256$"))
        login_request = Request(scope)
        login_payload = portal.PartnerLogin(email="newpartner@example.co.za", password="a-strong-password-123")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(portal.partner_login(login_payload, login_request))
        self.assertEqual(caught.exception.status_code, 403)
        approved = asyncio.run(portal.admin_update_partner_status(row[0],
            portal.PartnerStatus(status="ACTIVE"), "test-admin-key"))
        self.assertEqual(approved["status"], "ACTIVE")
        logged_in = asyncio.run(portal.partner_login(login_payload, login_request))
        self.assertTrue(logged_in["access_token"])
        os.environ.pop("NEXUSAI_ADMIN_KEY", None)

    def test_partner_cannot_submit_without_authentication(self):
        payload = portal.AddDeployment(
            site_reference="SITE-X",
            business_name="Retail X",
            camera_count=1,
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(portal.partner_submit_deployment(payload, None))
        self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
