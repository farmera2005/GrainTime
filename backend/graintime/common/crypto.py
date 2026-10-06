"""Encryption at rest for site database passwords.

The key comes from the secrets volume (see config.py) and is never stored in
the database. Ciphertext is Fernet (AES-128-CBC + HMAC-SHA256, authenticated).
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class DecryptionError(Exception):
    """The stored ciphertext cannot be read with the current key."""


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    try:
        return _fernet().decrypt(bytes(token)).decode("utf-8")
    except InvalidToken:
        raise DecryptionError(
            "Stored site password cannot be decrypted with the current encryption key; "
            "re-enter the password in the admin panel."
        ) from None
