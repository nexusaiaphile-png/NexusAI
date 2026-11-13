from backend.src import gmail_distributor_approvals as approvals


def _message(sender="NexusAI Admin <nexusai.aphile@gmail.com>", body="APPROVED\n\nThanks"):
    return {
        "labelIds": ["SENT"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": sender}],
            "body": {
                "data": __import__("base64").urlsafe_b64encode(body.encode()).decode().rstrip("=")
            },
        },
    }


def test_reply_command_accepts_owner_approval():
    assert approvals._reply_command(_message()) == "APPROVED"


def test_reply_command_accepts_owner_rejection():
    assert approvals._reply_command(_message(body="REJECTED\nNot suitable")) == "REJECTED"


def test_reply_command_rejects_other_sender():
    assert approvals._reply_command(_message(sender="attacker@example.com")) is None


def test_reply_command_rejects_non_sent_message():
    message = _message()
    message["labelIds"] = ["INBOX"]
    assert approvals._reply_command(message) is None


def test_reply_command_rejects_ambiguous_first_line():
    assert approvals._reply_command(_message(body="Please APPROVED this")) is None


def test_configured_requires_all_credentials_and_fixed_admin(monkeypatch):
    monkeypatch.setenv("NEXUSAI_GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("NEXUSAI_GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("NEXUSAI_GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("NEXUSAI_GMAIL_ACCOUNT", "nexusai.aphile@gmail.com")
    assert approvals.configured()
    monkeypatch.setenv("NEXUSAI_GMAIL_ACCOUNT", "other@example.com")
    assert not approvals.configured()
