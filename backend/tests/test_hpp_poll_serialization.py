"""Offline regression tests for HPP alarm-poll serialization.

These tests never call Hikvision, Render, customer data, or live notification services.
"""
import asyncio
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.src import main


class HppPollSerializationTests(unittest.TestCase):
    def setUp(self):
        self.hpp = sys.modules.get("hpp_integration")
        if self.hpp is None:
            self.skipTest("HPP integration module is not loaded in this test environment")
        self.route = next(
            route.endpoint
            for route in main.app.routes
            if getattr(route, "path", None) == "/api/hpp/alarms/poll"
        )

    def test_manual_poll_rejected_while_another_cycle_holds_lock(self):
        self.assertTrue(self.hpp._ALARM_CYCLE_LOCK.acquire(blocking=False))
        try:
            with patch.object(self.hpp, "_admin_guard"), patch.object(self.hpp, "poll_messages") as poll:
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(self.route(x_nexusai_admin_key="ci-test-key"))
            self.assertEqual(caught.exception.status_code, 409)
            poll.assert_not_called()
        finally:
            self.hpp._ALARM_CYCLE_LOCK.release()

    def test_failed_event_processing_does_not_acknowledge_batch(self):
        payload = {
            "data": {
                "batchId": "offline-test-batch",
                "list": [{"deviceSerial": "TEST-SERIAL", "alarmData": "{}"}],
            }
        }
        with patch.object(self.hpp, "acknowledge") as acknowledge:
            summary = self.hpp._process_batch(
                payload,
                lambda _event: (_ for _ in ()).throw(RuntimeError("simulated persistence failure")),
            )
        self.assertEqual(summary["received"], 1)
        self.assertEqual(len(summary["errors"]), 1)
        acknowledge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
