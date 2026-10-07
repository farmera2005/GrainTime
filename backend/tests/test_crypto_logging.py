import json
import logging

import pytest
from cryptography.fernet import Fernet

from graintime.common import crypto
from graintime.common.logging import JsonFormatter, redact
from graintime.common.secrets_init import ensure_secrets


def test_encrypt_roundtrip_and_ciphertext_hides_plaintext():
    token = crypto.encrypt("hunter2-site")
    assert b"hunter2" not in token
    assert crypto.decrypt(token) == "hunter2-site"


def test_decrypt_with_other_key_fails_cleanly():
    other = Fernet(Fernet.generate_key()).encrypt(b"x")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(other)


@pytest.mark.parametrize("text", [
    "DRIVER={x};SERVER=tcp:h,1433;UID=u;PWD={p@ss;w}}rd};Encrypt=yes",
    "UID=u;PWD=plainpw;Encrypt=yes",
    "postgresql+psycopg://graintime:plainpw@db:5432/graintime",
    '{"password": "plainpw"}',
])
def test_redaction(text):
    out = redact(text)
    assert "plainpw" not in out and "p@ss" not in out


def test_json_formatter_redacts_extra_fields():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "connecting %s", ("PWD=abc123;",), None)
    rec.conn = "UID=u;PWD=abc123"
    line = JsonFormatter("test").format(rec)
    assert "abc123" not in line
    assert json.loads(line)["service"] == "test"


def test_secrets_init_creates_once(tmp_path):
    created = ensure_secrets(tmp_path)
    assert set(created) == {"db_password", "encryption_key"}
    key = (tmp_path / "encryption_key").read_text()
    Fernet(key.encode())  # valid key
    assert ensure_secrets(tmp_path) == []
    assert (tmp_path / "encryption_key").read_text() == key


def test_secrets_init_main_runs(tmp_path, monkeypatch, capsys):
    from graintime.common import secrets_init
    monkeypatch.setattr(secrets_init, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(secrets_init.ensure_secrets, "__defaults__", (tmp_path, None))
    pub = tmp_path / "public"
    pub.mkdir()
    monkeypatch.setattr(secrets_init, "PUBLIC_SECRETS_DIR", str(pub))
    assert secrets_init.main() == 0
    assert "generated secrets" in capsys.readouterr().out
    # The public volume gets only the public role's password, never the others.
    assert sorted(p.name for p in pub.iterdir()) == ["public_db_password"]
    assert secrets_init.main() == 0
    assert "already present" in capsys.readouterr().out
