from datetime import datetime, timezone

import pytest

from backend.src import hpp_pos_correlation as correlation


def test_parse_hpp_iso_timestamp_to_utc():
    parsed = correlation._parse_timestamp("2026-11-02T10:15:30+02:00")
    assert parsed == datetime(2026, 11, 2, 8, 15, 30, tzinfo=timezone.utc)


def test_parse_hpp_epoch_milliseconds():
    parsed = correlation._parse_timestamp(1793614530000)
    assert parsed.tzinfo == timezone.utc


def test_snapshot_url_requires_https():
    assert correlation._snapshot_url({"data": {"url": "http://example.com/a.jpg"}}) == ""
    assert correlation._snapshot_url({"data": {"url": "https://cdn.example.com/a.jpg"}}) == "https://cdn.example.com/a.jpg"


def test_missing_timestamp_rejected():
    with pytest.raises(ValueError):
        correlation._parse_timestamp(None)


def test_correlate_hpp_event_fetches_and_persists_snapshot(monkeypatch):
    exception = {
        "id": 41,
        "site_id": "site-123456",
        "till_id": "till-4",
        "exception_type": "refund",
        "occurred_at": "2026-11-02T08:15:30Z",
        "camera_id": "DS123",
        "source_event_id": "pos-evt-41",
        "transaction_id": "tx-41",
    }
    calls = {}
    monkeypatch.setattr(correlation, "_find_pos_exception", lambda *args: exception)
    monkeypatch.setattr(correlation, "_persist_snapshot_link",
                        lambda *args: calls.setdefault("persist", args))

    from backend.src import hpp_integration
    monkeypatch.setattr(hpp_integration, "picture_url",
                        lambda path: {"data": {"url": "https://media.example.com/snapshot.jpg"}})

    event = {
        "site_id": "site-123456",
        "hpp_device_serial": "DS123",
        "timestamp": "2026-11-02T08:15:30Z",
        "hpp_data": {"happenTime": "2026-11-02T08:15:30Z", "pictureList": [{"id": "p1", "url": "ISAPI_FILES/alarm/41.jpg"}]},
    }
    result = correlation.correlate_hpp_event(event)

    assert result["status"] == "matched_snapshot_persisted"
    assert result["exception_id"] == 41
    assert calls["persist"][1] == "https://media.example.com/snapshot.jpg"
    assert calls["persist"][2] == "ISAPI_FILES/alarm/41.jpg"


def test_no_pos_match_does_not_request_picture(monkeypatch):
    monkeypatch.setattr(correlation, "_find_pos_exception", lambda *args: None)
    result = correlation.correlate_hpp_event({
        "site_id": "site-123456",
        "hpp_device_serial": "DS123",
        "hpp_data": {"happenTime": "2026-11-02T08:15:30Z", "filePath": "alarm/41.jpg"},
    })
    assert result["status"] == "no_pos_match"



def test_missing_vendor_event_time_is_skipped_not_matched(monkeypatch):
    def fail_if_queried(*args):
        raise AssertionError("POS lookup must not use the ingestion-time fallback")

    monkeypatch.setattr(correlation, "_find_pos_exception", fail_if_queried)
    result = correlation.correlate_hpp_event({
        "site_id": "site-123456",
        "hpp_device_serial": "DS123",
        "timestamp": "2026-11-02T08:15:30Z",
        "hpp_data": {"filePath": "alarm/41.jpg"},
    })
    assert result == {
        "status": "skipped",
        "reason": "missing_hpp_event_timestamp",
    }



def test_picture_list_direct_https_preserves_encrypt(monkeypatch):
    exception = {"id": 42, "site_id": "site-123456", "camera_id": "DS123"}
    saved = {}
    monkeypatch.setattr(correlation, "_find_pos_exception", lambda *args: exception)
    monkeypatch.setattr(correlation, "_persist_snapshot_link",
                        lambda *args: saved.setdefault("args", args))
    from backend.src import hpp_integration
    monkeypatch.setattr(hpp_integration, "picture_url",
                        lambda path: pytest.fail("direct HTTPS URL must not call pictureurl"))
    result = correlation.correlate_hpp_event({
        "site_id": "site-123456",
        "hpp_device_serial": "DS123",
        "hpp_data": {"happenTime": "2026-11-02T08:15:30Z",
                     "pictureList": [{"id": "p2", "url": "https://media.example/a.jpg?isEncrypted=1", "encrypt": 1}]},
    })
    assert result["status"] == "matched_encrypted_snapshot_persisted"
    assert result["previewable"] is False
    assert result["encrypt"] == 1
    assert saved["args"][1] == "https://media.example/a.jpg?isEncrypted=1"
    assert saved["args"][-1] == 1


def test_snapshot_details_preserves_encrypt_from_pictureurl_response():
    url, encrypt = correlation._snapshot_details({
        "data": {"pictureUrl": "https://media.example/a.jpg", "encrypt": 2}
    })
    assert url == "https://media.example/a.jpg"
    assert encrypt == 2


def test_camera_id_must_equal_hpp_device_serial(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): pass
        def fetchall(self):
            return [(1, "site-1", "2026-11-02T08:15:30Z", "DS123-2", "evt-1", True)]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return Cursor()
    monkeypatch.setattr(correlation, "_connect", lambda: Connection())
    assert correlation._find_pos_exception(
        "site-1", "DS123", datetime(2026, 11, 2, 8, 15, 30, tzinfo=timezone.utc)
    ) is None
