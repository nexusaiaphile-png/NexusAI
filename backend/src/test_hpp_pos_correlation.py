"""Offline regression tests for safe HPP-to-POS evidence matching."""

import unittest
from unittest.mock import patch

from backend.src import hpp_pos_correlation as correlation


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _FakeCursor(self.rows)


class HppPosCorrelationTests(unittest.TestCase):
    def _rows(self, timestamps):
        return [
            (index, "site-123456", stamp, "DEVICE-001", f"event-{index}", True)
            for index, stamp in enumerate(timestamps, start=1)
        ]

    def test_unique_nearest_exception_is_selected(self):
        rows = self._rows([
            "2026-11-24T10:00:00+00:00",
            "2026-11-24T10:00:04+00:00",
        ])
        with patch.object(correlation, "_connect", return_value=_FakeConnection(rows)):
            result = correlation._find_pos_exception(
                "site-123456", "DEVICE-001",
                correlation._parse_timestamp("2026-11-24T10:00:00.100+00:00"),
            )
        self.assertEqual(result["source_event_id"], "event-1")

    def test_equally_close_exceptions_are_rejected(self):
        rows = self._rows([
            "2026-11-24T10:00:00+00:00",
            "2026-11-24T10:00:00.200000+00:00",
        ])
        with patch.object(correlation, "_connect", return_value=_FakeConnection(rows)):
            with self.assertRaises(correlation.AmbiguousPOSMatch):
                correlation._find_pos_exception(
                    "site-123456", "DEVICE-001",
                    correlation._parse_timestamp("2026-11-24T10:00:00.100+00:00"),
                )

    def test_ambiguous_event_returns_status_without_creating_a_link(self):
        event = {
            "site_id": "site-123456",
            "hpp_device_serial": "DEVICE-001",
            "hpp_data": {"happenTime": "2026-11-24T10:00:00+00:00"},
        }
        with patch.object(
            correlation, "_find_pos_exception",
            side_effect=correlation.AmbiguousPOSMatch("ambiguous"),
        ), patch.object(correlation, "_persist_snapshot_link") as persist:
            result = correlation.correlate_hpp_event(event)
        self.assertEqual(result["status"], "ambiguous_pos_match")
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
