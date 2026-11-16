"""Sending email (scheduled reports, test messages).

Configured in the browser (Configuration -> Email) and stored in
app_settings["email"]. Secrets (client secret, SMTP password) are
Fernet-encrypted like site passwords, never returned by the api, never logged.

Two methods:
- "graph": Microsoft 365 through Microsoft Graph (client-credentials app
  registration with the Mail.Send application permission). Microsoft has been
  retiring password (basic) sign-in for SMTP, so this is the supported way
  for an unattended app to send from a Microsoft 365 mailbox.
- "smtp": any SMTP server or relay (Exchange / M365 connector, internal
  relay), STARTTLS, SSL or plain, optional sign-in.

TLS certificates are always verified. If the network inspects HTTPS with its
own certificate authority, its CA certificate can be pasted in the settings
and is trusted in addition to the system ones (never instead of verifying).
"""

from __future__ import annotations

import base64
import json
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import crypto

SETTINGS_KEY = "email"
TIMEOUT_S = 30
GRAPH_MAX_ATTACHMENT = 3 * 1024 * 1024        # sendMail with inline attachments
EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[A-Za-z]{2,}$")

DEFAULTS: dict = {
    "method": "graph",                 # graph | smtp
    "from_address": "",
    "from_name": "GrainTime",
    "link_base_url": "",               # e.g. http://graintime:8080 (links in emails)
    # graph
    "tenant_id": "", "client_id": "", "client_secret_encrypted": None,
    # smtp
    "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",   # starttls | ssl | none
    "smtp_username": "", "smtp_password_encrypted": None,
    # both
    "ca_cert_pem": None,
}


class MailError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime: str = "application/octet-stream"


def config_with_defaults(stored: dict | None) -> dict:
    return {**DEFAULTS, **(stored or {})}


def configured(cfg: dict) -> bool:
    if not cfg.get("from_address"):
        return False
    if cfg["method"] == "graph":
        return bool(cfg["tenant_id"] and cfg["client_id"] and cfg.get("client_secret_encrypted"))
    return bool(cfg["smtp_host"])


def valid_address(a: str) -> bool:
    return bool(EMAIL_RE.match(a.strip()))


def _secret(cfg: dict, key: str) -> str:
    v = cfg.get(key)
    return crypto.decrypt(v.encode()) if v else ""


def ssl_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if cfg.get("ca_cert_pem"):
        ctx.load_verify_locations(cadata=cfg["ca_cert_pem"])
    return ctx


def _tls_hint(exc: Exception) -> MailError | None:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return MailError("certificate_untrusted",
                         "The mail server's certificate is not trusted. If your network inspects HTTPS, paste "
                         "its CA certificate under Email settings (verification stays on).")
    return None


# --------------------------------------------------------------------------- #
# Microsoft Graph
# --------------------------------------------------------------------------- #

class _TokenCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.tokens: dict[tuple, tuple[str, float]] = {}

    def get(self, key):
        with self.lock:
            t = self.tokens.get(key)
            return t[0] if t and t[1] > time.time() + 60 else None

    def put(self, key, token, expires_in):
        with self.lock:
            self.tokens[key] = (token, time.time() + int(expires_in))


_tokens = _TokenCache()


def _http(method: str, url: str, ctx: ssl.SSLContext, body: bytes | None = None, headers: dict | None = None,
          opener=None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        if opener:
            return opener(req)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        hint = _tls_hint(e.reason if isinstance(e.reason, Exception) else e)
        raise hint or MailError("unreachable", f"Could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None


def _graph_error(raw: bytes) -> str:
    try:
        d = json.loads(raw)
        err = d.get("error")
        if isinstance(err, dict):
            return f"{err.get('code')}: {err.get('message')}"
        return f"{err}: {d.get('error_description', '')}".strip()
    except ValueError:
        return raw[:300].decode(errors="replace")


def graph_token(cfg: dict, opener=None) -> str:
    key = (cfg["tenant_id"], cfg["client_id"])
    tok = _tokens.get(key)
    if tok:
        return tok
    body = urllib.parse.urlencode({"client_id": cfg["client_id"], "client_secret": _secret(cfg, "client_secret_encrypted"),
                                   "scope": "https://graph.microsoft.com/.default",
                                   "grant_type": "client_credentials"}).encode()
    url = f"https://login.microsoftonline.com/{urllib.parse.quote(cfg['tenant_id'])}/oauth2/v2.0/token"
    status, raw = _http("POST", url, ssl_context(cfg), body,
                        {"Content-Type": "application/x-www-form-urlencoded"}, opener)
    if status != 200:
        detail = _graph_error(raw)
        code = "graph_sign_in_failed"
        if "AADSTS7000215" in detail or "invalid_client" in detail:
            detail = "the client secret is wrong or has expired (" + detail[:200] + ")"
        elif "AADSTS700016" in detail:
            detail = "no app with that client (application) ID in this tenant (" + detail[:200] + ")"
        elif "AADSTS90002" in detail:
            detail = "no tenant with that ID (" + detail[:200] + ")"
        raise MailError(code, f"Microsoft 365 sign-in failed: {detail}")
    d = json.loads(raw)
    _tokens.put(key, d["access_token"], d.get("expires_in", 3600))
    return d["access_token"]


def send_graph(cfg: dict, to: list[str], subject: str, html: str, text: str,
               attachments: list[Attachment], opener=None) -> None:
    token = graph_token(cfg, opener)
    msg = {"subject": subject, "body": {"contentType": "HTML", "content": html},
           "toRecipients": [{"emailAddress": {"address": a}} for a in to]}
    if attachments:
        msg["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": a.filename,
                               "contentType": a.mime, "contentBytes": base64.b64encode(a.content).decode()}
                              for a in attachments]
    sender = urllib.parse.quote(cfg["from_address"])
    status, raw = _http("POST", f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail", ssl_context(cfg),
                        json.dumps({"message": msg, "saveToSentItems": True}).encode(),
                        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, opener)
    if status != 202:
        detail = _graph_error(raw)
        if status == 403 or "ErrorAccessDenied" in detail:
            raise MailError("graph_forbidden", "Microsoft 365 refused to send: the app needs the Mail.Send "
                            f"application permission with admin consent, and may be limited to certain mailboxes "
                            f"({detail[:200]}).")
        if status == 404 or "ErrorInvalidUser" in detail or "MailboxNotEnabled" in detail:
            raise MailError("graph_mailbox", f"No mailbox {cfg['from_address']} in Microsoft 365 ({detail[:200]}).")
        raise MailError("graph_failed", f"Microsoft 365 did not send the message ({status}: {detail[:300]}).")


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #

def send_smtp(cfg: dict, to: list[str], subject: str, html: str, text: str,
              attachments: list[Attachment]) -> None:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = formataddr((cfg.get("from_name") or "GrainTime", cfg["from_address"]))
    m["To"] = ", ".join(to)
    m["Message-ID"] = make_msgid(domain=cfg["from_address"].split("@")[-1])
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    for a in attachments:
        maintype, _, subtype = a.mime.partition("/")
        m.add_attachment(a.content, maintype=maintype, subtype=subtype or "octet-stream", filename=a.filename)
    host, port, sec = cfg["smtp_host"], int(cfg["smtp_port"]), cfg["smtp_security"]
    try:
        if sec == "ssl":
            s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S, context=ssl_context(cfg))
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT_S)
        with s:
            s.ehlo()
            if sec == "starttls":
                s.starttls(context=ssl_context(cfg))
                s.ehlo()
            if cfg.get("smtp_username"):
                s.login(cfg["smtp_username"], _secret(cfg, "smtp_password_encrypted"))
            s.send_message(m)
    except smtplib.SMTPAuthenticationError as exc:
        hint = ""
        if "smtp.office365.com" in host.lower() or "5.7.139" in str(exc) or "basic authentication" in str(exc).lower():
            hint = (" Microsoft 365 no longer accepts password sign-in for SMTP in most tenants; use the Microsoft "
                    "365 (Graph) method, or a relay connector without sign-in.")
        raise MailError("smtp_auth_failed", f"The mail server refused the sign-in ({exc.smtp_code}).{hint}") from None
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError("smtp_recipients", f"The mail server refused the recipients: {', '.join(exc.recipients)}") from None
    except smtplib.SMTPNotSupportedError:
        raise MailError("smtp_starttls", "The mail server doesn't offer STARTTLS. Choose SSL, or 'none' only for an "
                                         "internal relay.") from None
    except smtplib.SMTPException as exc:
        raise MailError("smtp_failed", f"The mail server did not accept the message: {exc}") from None
    except (ssl.SSLError, OSError) as exc:
        hint = _tls_hint(exc)
        if hint:
            raise hint from None
        raise MailError("unreachable", f"Could not reach the mail server {host}:{port}: {exc}") from None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def send(cfg: dict, to: list[str], subject: str, html: str, text: str,
         attachments: list[Attachment] | None = None, opener=None) -> None:
    to = [a.strip() for a in to if a and a.strip()]
    if not to:
        raise MailError("no_recipients", "No recipients.")
    bad = [a for a in to if not valid_address(a)]
    if bad:
        raise MailError("bad_recipients", f"Not email addresses: {', '.join(bad)}")
    if not configured(cfg):
        raise MailError("not_configured", "Email is not set up. An administrator can set it up under "
                                          "Configuration → Email.")
    attachments = attachments or []
    if cfg["method"] == "graph":
        big = [a for a in attachments if len(a.content) > GRAPH_MAX_ATTACHMENT]
        if big:
            attachments = [a for a in attachments if a not in big]
            html += ("<p style='color:#5c5c63;font-size:12px'>The attachment "
                     f"{', '.join(a.filename for a in big)} was too large to email; open the report in GrainTime "
                     "to download it.</p>")
        send_graph(cfg, to, subject, html, text, attachments, opener)
    else:
        send_smtp(cfg, to, subject, html, text, attachments)
