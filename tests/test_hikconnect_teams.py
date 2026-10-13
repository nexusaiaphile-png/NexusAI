"""Offline validation tests for the Hik-Connect for Teams adapter."""
import os
import unittest
from unittest.mock import patch

from backend.src.hikconnect_teams import (
    HikConnectTeamsClient,
    HikConnectTeamsError,
    TOKEN_BASE_URL,
    TOKEN_PATH,
    MESSAGES_PATH,
    normalize_message,
    process_batch,
)


class FakeClient:
    def __init__(self):
        self.acked = []

    def acknowledge(self, batch_id):
        self.acked.append(batch_id)
        return {"errorCode": "0"}


class HikConnectTeamsTests(unittest.TestCase):
    def test_auth_uses_documented_ieu_host_and_path(self):
        client = HikConnectTeamsClient("test-app-key", "test-secret")
        response = {
            "errorCode": "0",
            "data": {
                "accessToken": "test-access-token",
                "expireTime": 2_000_000_000,
                "areaDomain": "https://ieu-team.hikcentralconnect.com",
            },
        }
        with patch.object(client, "_post", return_value=response) as post:
            result = client.authenticate()
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["area_domain"], "https://ieu-team.hikcentralconnect.com")
        args, kwargs = post.call_args
        self.assertEqual(args[0], TOKEN_BASE_URL)
        self.assertEqual(args[1], TOKEN_PATH)
        self.assertEqual(args[2], {"appKey": "test-app-key", "secretKey": "test-secret"})

    def test_authorized_post_returns_vendor_response(self):
        client = HikConnectTeamsClient("app", "secret")
        client._token = "access"
        client._token_expiry = 4_000_000_000
        client._area_domain = "https://ieu-team.hikcentralconnect.com"
        response = {"errorCode": "0", "data": {"messages": []}}
        with patch.object(client, "_post", return_value=response) as post:
            result = client._authorized_post(MESSAGES_PATH, {"subscriptionId": "sub-1", "maxMessages": 10})
        self.assertEqual(result, response)
        self.assertEqual(post.call_args.args[0], "https://ieu-team.hikcentralconnect.com")
        self.assertEqual(post.call_args.kwargs["token"], "access")

    def test_normalize_message_maps_documented_fields(self):
        event = normalize_message({
            "eventId": "evt-1", "siteId": "site-1", "deviceId": "dev-1",
            "deviceName": "Front door NVR", "channelId": "2", "channelName": "Front door",
            "eventType": 7, "eventTypeName": "Motion", "severity": "high",
            "timestamp": 1_760_000_000, "picUrl": "https://example.invalid/image.jpg",
        })
        self.assertEqual(event["hikconnect_event_id"], "evt-1")
        self.assertEqual(event["camera_id"], "dev-1-2")
        self.assertEqual(event["event"], "Motion")
        self.assertEqual(event["severity"], "HIGH")
        self.assertTrue(event["snapshot_available"])
        self.assertEqual(event["source"], "hikconnect-teams")

    def test_batch_is_acknowledged_only_after_durable_sink_success(self):
        client = FakeClient()
        result = {"errorCode": "0", "data": {"batchId": "batch-1", "messages": [
            {"eventId": "evt-1", "timestamp": 1_760_000_000}
        ]}}
        summary = process_batch(result, lambda event: True, client)
        self.assertTrue(summary["acknowledged"])
        self.assertEqual(client.acked, ["batch-1"])

    def test_failed_sink_prevents_acknowledgement(self):
        client = FakeClient()
        result = {"errorCode": "0", "data": {"batchId": "batch-2", "messages": [
            {"eventId": "evt-2", "timestamp": 1_760_000_000}
        ]}}
        summary = process_batch(result, lambda event: False, client)
        self.assertFalse(summary["acknowledged"])
        self.assertEqual(client.acked, [])
        self.assertEqual(summary["processed"], 0)

    def test_nonempty_batch_without_batch_id_is_rejected(self):
        client = FakeClient()
        result = {"errorCode": "0", "data": {"messages": [
            {"eventId": "evt-3", "timestamp": 1_760_000_000}
        ]}}
        with self.assertRaises(HikConnectTeamsError):
            process_batch(result, lambda event: True, client)
        self.assertEqual(client.acked, [])


if __name__ == "__main__":
    unittest.main()
