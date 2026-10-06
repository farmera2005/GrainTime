"""One-shot first-start secret generation (the `init` Compose service).

Creates any missing secret in the secrets volume and leaves existing ones
untouched, so it is safe to run on every start. Nobody types these values:

  db_password      password of the central PostgreSQL owner role
  encryption_key   Fernet key that encrypts site database passwords at rest
                   (stored only in this volume, never in the database)

Losing encryption_key does not lose any data, but every site password then
has to be re-entered in the admin panel. Back up the secrets volume along with
the database volume.
"""

from __future__ import annotations

import os
import secrets
import sys

from cryptography.fernet import Fernet

from .config import DB_PASSWORD_FILE, ENCRYPTION_KEY_FILE, SECRETS_DIR
from .logging import get_logger, setup_logging

GENERATORS = {
    DB_PASSWORD_FILE: lambda: secrets.token_urlsafe(32),
    ENCRYPTION_KEY_FILE: lambda: Fernet.generate_key().decode(),
}


def ensure_secrets(directory=SECRETS_DIR) -> list[str]:
    os.makedirs(directory, mode=0o755, exist_ok=True)
    created = []
    for name, gen in GENERATORS.items():
        path = os.path.join(directory, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
        with os.fdopen(fd, "w") as f:
            f.write(gen())
        os.replace(tmp, path)
        created.append(name)
    return created


def main() -> int:
    setup_logging("init")
    log = get_logger("init")
    created = ensure_secrets()
    if created:
        log.info("generated secrets", extra={"generated": created})
    else:
        log.info("secrets already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
