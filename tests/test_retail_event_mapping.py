"""Retail Intelligence must not turn camera-only alarms into security alerts."""

from backend.src.retail_event_mapping import normalize_retail_event


def test_camera_only_threat_weapon_and_theft_events_are_dropped():
    for event_name in (
        "weapon_detected",
        "panic_button_pressed",
        "shoplifting_detected",
        "intrusion",
        "motion",
    ):
        assert normalize_retail_event({"event": event_name, "severity": "CRITICAL"}) is None


def test_hpp_event_with_generic_metadata_is_dropped_after_correlation():
    event = {
        "event": "HIKVISION_ALARM",
        "eventType": "lineCross",
        "hpp_device_serial": "HPP-DEVICE-001",
        "pos_correlation": {"status": "no_matching_pos_exception"},
    }
    assert normalize_retail_event(event) is None


def test_invalid_input_is_rejected():
    try:
        normalize_retail_event("not-an-event")
    except TypeError as exc:
        assert "dictionary" in str(exc)
    else:
        raise AssertionError("Expected non-dictionary input to be rejected.")
