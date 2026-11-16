"""Email: SMTP against a real local server, Microsoft Graph against a mock."""
import json
from email import message_from_bytes

import pytest

from graintime.common import crypto, mailer


@pytest.fixture
def smtp_server():
    from aiosmtpd.controller import Controller

    class Handler:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(envelope)
            return "250 OK"
    import socket
    with socket.socket() as sock:              # a free port (aiosmtpd can't take port 0)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    h = Handler()
    c = Controller(h, hostname="127.0.0.1", port=port)
    c.start()
    try:
        yield port, h
    finally:
        c.stop()


def smtp_cfg(port, **kw):
    return mailer.config_with_defaults({"method": "smtp", "from_address": "graintime@mercerlandmark.com",
                                        "smtp_host": "127.0.0.1", "smtp_port": port, "smtp_security": "none", **kw})


def test_smtp_sends_html_text_and_attachment(smtp_server):
    port, h = smtp_server
    mailer.send(smtp_cfg(port), ["ops@mercerlandmark.com", "gm@mercerlandmark.com"], "Daily report",
                "<p>Hello <b>report</b></p>", "Hello report",
                [mailer.Attachment("trucks.xlsx", b"PK\x03\x04data", "application/vnd.ms-excel")])
    env = h.messages[0]
    assert env.rcpt_tos == ["ops@mercerlandmark.com", "gm@mercerlandmark.com"]
    msg = message_from_bytes(env.content)
    assert msg["Subject"] == "Daily report" and "GrainTime <graintime@mercerlandmark.com>" in msg["From"]
    parts = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in parts and "text/html" in parts and "application/vnd.ms-excel" in parts


def test_smtp_unreachable_and_bad_recipients():
    with pytest.raises(mailer.MailError) as e:
        mailer.send(smtp_cfg(1), ["a@b.co"], "s", "h", "t")
    assert e.value.code == "unreachable"
    with pytest.raises(mailer.MailError) as e:
        mailer.send(smtp_cfg(1), ["not-an-address"], "s", "h", "t")
    assert e.value.code == "bad_recipients"
    with pytest.raises(mailer.MailError) as e:
        mailer.send(mailer.config_with_defaults({}), ["a@b.co"], "s", "h", "t")
    assert e.value.code == "not_configured"


def graph_cfg(**kw):
    return mailer.config_with_defaults({"method": "graph", "from_address": "graintime@mercerlandmark.com",
                                        "tenant_id": "11111111-2222-3333-4444-555555555555",
                                        "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                        "client_secret_encrypted": crypto.encrypt("s3cret").decode(), **kw})


class FakeGraph:
    def __init__(self, token_status=200, send_status=202, send_body=b""):
        self.calls, self.token_status, self.send_status, self.send_body = [], token_status, send_status, send_body

    def __call__(self, req):
        body = req.data
        self.calls.append((req.full_url, dict(req.header_items()), body))
        if "login.microsoftonline.com" in req.full_url:
            if self.token_status != 200:
                return self.token_status, json.dumps({"error": "invalid_client",
                                                      "error_description": "AADSTS7000215: Invalid client secret"}).encode()
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        return self.send_status, self.send_body


@pytest.fixture(autouse=True)
def fresh_tokens():
    mailer._tokens.tokens.clear()


def test_graph_signs_in_and_sends_with_attachment():
    g = FakeGraph()
    mailer.send(graph_cfg(), ["gm@mercerlandmark.com"], "Weekly report", "<p>hi</p>", "hi",
                [mailer.Attachment("t.xlsx", b"PK", "application/x")], opener=g)
    token_url, _, token_body = g.calls[0]
    assert "/11111111-2222-3333-4444-555555555555/oauth2/v2.0/token" in token_url
    assert b"grant_type=client_credentials" in token_body and b"client_secret=s3cret" in token_body
    send_url, headers, body = g.calls[1]
    assert send_url == "https://graph.microsoft.com/v1.0/users/graintime%40mercerlandmark.com/sendMail"
    assert headers["Authorization"] == "Bearer tok"
    msg = json.loads(body)["message"]
    assert msg["toRecipients"][0]["emailAddress"]["address"] == "gm@mercerlandmark.com"
    assert msg["attachments"][0]["name"] == "t.xlsx" and msg["body"]["contentType"] == "HTML"
    mailer.send(graph_cfg(), ["gm@mercerlandmark.com"], "again", "<p/>", "", opener=g)
    assert sum("login.microsoftonline.com" in c[0] for c in g.calls) == 1      # token reused


@pytest.mark.parametrize("fake,code,words", [
    (FakeGraph(token_status=401), "graph_sign_in_failed", "client secret is wrong or has expired"),
    (FakeGraph(send_status=403, send_body=b'{"error":{"code":"ErrorAccessDenied","message":"Access is denied."}}'),
     "graph_forbidden", "Mail.Send"),
    (FakeGraph(send_status=404, send_body=b'{"error":{"code":"ErrorInvalidUser","message":"x"}}'),
     "graph_mailbox", "No mailbox"),
])
def test_graph_failures_are_explained(fake, code, words):
    with pytest.raises(mailer.MailError) as e:
        mailer.send(graph_cfg(), ["gm@mercerlandmark.com"], "s", "h", "t", opener=fake)
    assert e.value.code == code and words in e.value.message


def test_graph_drops_oversized_attachment_with_a_note():
    g = FakeGraph()
    mailer.send(graph_cfg(), ["gm@mercerlandmark.com"], "s", "<p>x</p>", "t",
                [mailer.Attachment("big.xlsx", b"0" * (mailer.GRAPH_MAX_ATTACHMENT + 1))], opener=g)
    msg = json.loads(g.calls[-1][2])["message"]
    assert "attachments" not in msg and "too large to email" in msg["body"]["content"]
