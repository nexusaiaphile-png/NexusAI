from datetime import datetime, timezone

import pytest

from backend.src.hpp_pos_correlation import _parse_timestamp, _snapshot_url


def test_parse_hpp_iso_timestamp_to_utc():
    parsed = _parse_timestamp("2026-11-02T10:15:30+02:00")
    assert parsed == datetime(2026, 11, 2, 8, 15, 30, tzinfo=timezone.utc)


def test_parse_hpp_epoch_milliseconds():
    parsed = _parse_timestamp(1793614530000)
    assert parsed.tzinfo is timezone.utc


def test_snapshot_url_requires_https():
    assert _snapshot_url({"data": {"url": "http://example.com/a.jpg"}}) == ""
    assert _snapshot_url({"data": {"url": "https://cdn.example.com/a.jpg"}}) == "https://cdn.example.com/a.jpg"


def test_missing_timestamp_rejected():
    with pytest.raises(ValueError):
        _parse_timestamp(None)
