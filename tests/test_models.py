"""Offline validation tests for POS integration request models."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from backend.src.models import (
    POSExceptionBatchIn,
    POSExceptionEventIn,
    VideoTimelineReferenceIn,
)


def valid_event(**overrides):
    payload = {
        "site_id": "spar-site-001",
        "source_event_id": "pos-evt-0001",
        "till_id": "lane-04",
        "exception_type": "refund",
        "cashier_id": "cashier-12",
        "cashier_name": "Test Cashier",
        "transaction_value": "125.50",
        "currency": "zar",
        "occurred_at": "2026-10-19T14:30:00+02:00",
        "metadata": {"payment_method": "card"},
    }
    payload.update(overrides)
    return payload


class POSExceptionModelTests(unittest.TestCase):
    def test_accepts_flat_vendor_payload_and_normalizes_currency(self):
        event = POSExceptionEventIn.model_validate(valid_event())
        self.assertEqual(event.site_id, "spar-site-001")
        self.assertEqual(event.cashier_id, "cashier-12")
        self.assertEqual(event.transaction_value, Decimal("125.50"))
        self.assertEqual(event.currency, "ZAR")
        self.assertEqual(event.occurred_at.utcoffset().total_seconds(), 7200)

    def test_requires_explicit_timezone_offset(self):
        with self.assertRaises(ValidationError):
            POSExceptionEventIn.model_validate(valid_event(occurred_at="2026-10-19T14:30:00"))

    def test_rejects_unknown_exception_type(self):
        with self.assertRaises(ValidationError):
            POSExceptionEventIn.model_validate(valid_event(exception_type="theft"))

    def test_requires_source_event_id(self):
        payload = valid_event()
        payload.pop("source_event_id")
        with self.assertRaises(ValidationError):
            POSExceptionEventIn.model_validate(payload)

    def test_rejects_unknown_top_level_fields(self):
        with self.assertRaises(ValidationError):
            POSExceptionEventIn.model_validate(valid_event(unreviewed_customer_data="not allowed"))

    def test_batch_rejects_duplicate_site_event_keys(self):
        event = valid_event()
        with self.assertRaises(ValidationError):
            POSExceptionBatchIn.model_validate({"events": [event, event]})

    def test_batch_accepts_distinct_events(self):
        first = valid_event()
        second = valid_event(source_event_id="pos-evt-0002")
        batch = POSExceptionBatchIn.model_validate({"events": [first, second]})
        self.assertEqual(len(batch.events), 2)

    def test_video_reference_requires_https_and_ordered_timezone_aware_times(self):
        payload = {
            "site_id": "spar-site-001",
            "camera_id": "cam-05",
            "clip_start": "2026-10-19T14:30:00+02:00",
            "clip_end": "2026-10-19T14:31:00+02:00",
            "video_clip_url": "https://example.invalid/clip/1",
            "source_ref_id": "clip-0001",
        }
        self.assertEqual(VideoTimelineReferenceIn.model_validate(payload).camera_id, "cam-05")
        with self.assertRaises(ValidationError):
            VideoTimelineReferenceIn.model_validate({**payload, "video_clip_url": "http://example.invalid/clip/1"})
        with self.assertRaises(ValidationError):
            VideoTimelineReferenceIn.model_validate({**payload, "clip_end": "2026-10-19T14:29:00+02:00"})


if __name__ == "__main__":
    unittest.main()
