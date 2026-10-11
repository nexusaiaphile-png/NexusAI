"""Regression tests for the HPP alarm and Retail Intelligence fixes.

These tests use mocked HPP calls and a temporary SQLite database. They never
contact Hik-Partner Pro or send production POS/camera events.
"""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from backend.src import hpp_integration as hpp
from backend.src import retail_intelligence as retail


class HppRegressionTests(unittest.TestCase):
    def setUp(self):
        self._old_admin = os.environ.get("NEXUSAI_ADMIN_KEY")
        os.environ["NEXUSAI_ADMIN_KEY"] = "unit-test-admin-key"

    def tearDown(self):
        if self._old_admin is None:
            os.environ.pop("NEXUSAI_ADMIN_KEY", None)
        else:
            os.environ["NEXUSAI_ADMIN_KEY"] = self._old_admin

    def test_manual_poll_returns_409_while_cycle_lock_is_held(self):
        app = FastAPI()
        hpp.install(app, event_sink=lambda event: True)
        endpoint = next(
            route.endpoint for route in app.routes
            if getattr(route, "path", None) == "/api/hpp/alarms/poll"
        )
        self.assertTrue(hpp._ALARM_CYCLE_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(endpoint(x_nexusai_admin_key="unit-test-admin-key"))
            self.assertEqual(raised.exception.status_code, 409)
        finally:
            hpp._ALARM_CYCLE_LOCK.release()

    def test_batch_is_not_acknowledged_when_event_processing_raises(self):
        def fail_processing(event):
            raise RuntimeError("simulated processing failure")

        result = {"data": {"batchId": "mock-batch-1", "list": [{"alarmData": {}}]}}
        with patch.object(hpp, "acknowledge") as acknowledge:
            summary = hpp._process_batch(result, fail_processing)
        self.assertEqual(summary["errors"], ["simulated processing failure"])
        acknowledge.assert_not_called()


class RetailIncidentRegressionTests(unittest.TestCase):
    def test_incidents_endpoint_returns_empty_and_stored_incidents(self):
        old_admin = os.environ.get("NEXUSAI_ADMIN_KEY")
        os.environ["NEXUSAI_ADMIN_KEY"] = "unit-test-admin-key"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "retail-test.sqlite3"
            with patch.object(retail, "DB_PATH", db_path):
                app = FastAPI()
                retail.install(app)
                endpoint = next(
                    route.endpoint for route in app.routes
                    if getattr(route, "path", None) == "/api/retail/incidents"
                )

                empty = asyncio.run(endpoint(
                    status="OPEN", limit=50,
                    x_nexusai_admin_key="unit-test-admin-key",
                ))
                self.assertEqual(empty, {"incidents": []})

                now = 1234567890.0
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        """INSERT INTO retail_incidents
                        (id, site_id, lane_id, transaction_id, event_id, status,
                         summary, evidence_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("inc-test-1", "site-test-001", "lane-1", "tx-test-1",
                         "event-test-1", "OPEN", "Test incident", '{"source":"unit-test"}',
                         now, now),
                    )

                populated = asyncio.run(endpoint(
                    status="OPEN", limit=50,
                    x_nexusai_admin_key="unit-test-admin-key",
                ))
                self.assertEqual(len(populated["incidents"]), 1)
                incident = populated["incidents"][0]
                self.assertEqual(incident["id"], "inc-test-1")
                self.assertEqual(incident["evidence"], {"source": "unit-test"})
                self.assertEqual(incident["status"], "OPEN")

        if old_admin is None:
            os.environ.pop("NEXUSAI_ADMIN_KEY", None)
        else:
            os.environ["NEXUSAI_ADMIN_KEY"] = old_admin


if __name__ == "__main__":
    unittest.main()
