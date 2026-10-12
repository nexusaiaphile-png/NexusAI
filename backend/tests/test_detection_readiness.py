"""Offline regression tests for the HPP-to-NexusAI event sink.

These tests mock persistence and notification dispatch. They never contact Hikvision,
Render, a customer database, or a real push-notification endpoint.
"""
import unittest
from unittest.mock import patch

from backend.src import main


class HppEventSinkReadinessTests(unittest.TestCase):
    def setUp(self):
        self.original_events = list(main.EDGE_EVENTS)
        main.EDGE_EVENTS.clear()

    def tearDown(self):
        main.EDGE_EVENTS[:] = self.original_events

    def sample_event(self):
        return {
            "hpp_site_id": "hpp-test-site",
            "hpp_device_serial": "TEST-SERIAL-001",
            "camera_id": "channel-1",
            "camera_name": "Test camera",
            "location": "Offline test site",
            "event": "line_crossing",
            "severity": "HIGH",
            "timestamp": "2026-10-11T12:00:00Z",
            "source": "hikvision_hpp",
        }

    def test_unmapped_device_fails_closed_without_persist_or_push(self):
        event = self.sample_event()
        with patch.object(main, "_hpp_mapping", return_value=None), \
             patch.object(main, "persist_event") as persist, \
             patch.object(main.PUSH_EXECUTOR, "submit") as submit:
            with self.assertRaisesRegex(RuntimeError, "No NexusAI mapping"):
                main._hpp_event_sink(event)
        persist.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(main.EDGE_EVENTS, [])

    def test_persistence_failure_does_not_queue_push_or_publish_event(self):
        event = self.sample_event()
        with patch.object(main, "_hpp_mapping", return_value="site-test123"), \
             patch.object(main, "persist_event", side_effect=RuntimeError("database unavailable")), \
             patch.object(main.PUSH_EXECUTOR, "submit") as submit:
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                main._hpp_event_sink(event)
        submit.assert_not_called()
        self.assertEqual(main.EDGE_EVENTS, [])

    def test_successful_event_is_persisted_before_push_is_queued(self):
        event = self.sample_event()
        call_order = []

        def fake_persist(value):
            call_order.append(("persist", value["site_id"]))
            return 42

        def fake_submit(fn, value):
            call_order.append(("push_queued", value["site_id"]))
            return object()

        with patch.object(main, "_hpp_mapping", return_value="site-test123"), \
             patch.object(main, "persist_event", side_effect=fake_persist), \
             patch.object(main.PUSH_EXECUTOR, "submit", side_effect=fake_submit):
            result = main._hpp_event_sink(event)

        self.assertTrue(result)
        self.assertEqual(call_order, [("persist", "site-test123"), ("push_queued", "site-test123")])
        self.assertNotIn("id", main.EDGE_EVENTS[0])
        self.assertEqual(main.EDGE_EVENTS[0]["site_id"], "site-test123")
        self.assertNotIn("hpp_site_id", main.EDGE_EVENTS[0])


if __name__ == "__main__":
    unittest.main()
