"""
Runtime settings for the Policy & Risk Guardrails PoC.

- Reads from environment (supports a local .env via python-dotenv).
- Provides a single import: `from app.core.config import settings`.

Required (for DB):
  DATABASE_URL = postgresql+asyncpg://USER:PASS@HOST:5432/DBNAME

Optional:
  APP_ENV=dev|prod            (default: dev)
  APP_TIMEZONE=Asia/Dubai     (default: Asia/Dubai)
  CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
  LOG_LEVEL=INFO|DEBUG|WARNING|ERROR (default: DEBUG in dev, INFO in prod)
"""

from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

try:
    import pytz
except Exception:
    pytz = None  # soft dependency; FastAPI will still run

# Load .env once at import time (safe no-op if file missing)
load_dotenv()


def _csv_env(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings:
    # --- Core ---
    DATABASE_URL: str
    APP_ENV: str
    APP_TIMEZONE: str
    CORS_ORIGINS: List[str]
    LOG_LEVEL: str
    DEBUG: bool

    def __init__(self) -> None:
        # Database (must use asyncpg driver)
        self.DATABASE_URL = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
        )

        # Environment
        self.APP_ENV = os.getenv("APP_ENV", "dev").lower()
        self.DEBUG = self.APP_ENV != "prod"

        # Timezone
        tz = os.getenv("APP_TIMEZONE", "Asia/Dubai")
        if pytz:
            try:
                _ = pytz.timezone(tz)  # validate
            except Exception:
                tz = "Asia/Dubai"
        self.APP_TIMEZONE = tz

        # CORS
        self.CORS_ORIGINS = _csv_env(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        )

        # Logging
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if self.DEBUG else "INFO").upper()

        # Guardrail: help catch sync drivers early (session.py also validates)
        if "asyncpg" not in self.DATABASE_URL:
            # Do not raise here (keeps unit tests simple); session.py raises if invalid.
            pass


# Singleton
settings = Settings()
