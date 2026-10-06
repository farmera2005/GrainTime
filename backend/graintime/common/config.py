"""Infrastructure settings.

Nothing here has to be configured by hand. The `init` service generates the
central database password and the site-password encryption key into the
secrets volume on first start, and every other value has a working default for
the Compose stack. Environment variables exist only as optional overrides
(tests, unusual deployments). Sites, users and all application settings live
in the central database and are managed in the browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

SECRETS_DIR = Path(os.environ.get("GRAINTIME_SECRETS_DIR", "/run/graintime-secrets"))

DB_PASSWORD_FILE = "db_password"
ENCRYPTION_KEY_FILE = "encryption_key"


def read_secret(name: str) -> str:
    path = SECRETS_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"Secret '{name}' not found in {SECRETS_DIR}. The init service creates it on "
            "first start; check that it ran and that the secrets volume is mounted."
        ) from None


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_key: str
    # Minutes after the api starts during which the first administrator can be
    # created from the browser. Guards against someone else on the network
    # claiming a fresh install. Restarting the api reopens the window, and only
    # while no user exists yet.
    setup_window_minutes: int = 60
    session_hours: int = 12


@lru_cache
def get_settings() -> Settings:
    url = os.environ.get("GRAINTIME_DATABASE_URL")
    if not url:
        host = os.environ.get("GRAINTIME_DB_HOST", "db")
        port = os.environ.get("GRAINTIME_DB_PORT", "5432")
        name = os.environ.get("GRAINTIME_DB_NAME", "graintime")
        user = os.environ.get("GRAINTIME_DB_USER", "graintime")
        password = quote(read_secret(DB_PASSWORD_FILE), safe="")
        url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"
    key = os.environ.get("GRAINTIME_ENCRYPTION_KEY") or read_secret(ENCRYPTION_KEY_FILE)
    return Settings(
        database_url=url,
        encryption_key=key,
        setup_window_minutes=int(os.environ.get("GRAINTIME_SETUP_WINDOW_MINUTES", "60")),
    )
