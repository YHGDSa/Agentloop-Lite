"""Runtime settings, read once from environment variables at import time.

Every value has a safe local default so the service can boot without a .env;
secrets (LLM_API_KEY) have no default and are never written to the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# A local .env is optional: without python-dotenv (or without the file) the
# process falls back to real environment variables, which is what CI and the
# tests use. Values already in the environment always win (override=False).
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back when it is unset or blank."""
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process configuration."""

    db_path: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    max_steps: int
    approval_timeout_seconds: int
    max_attempts: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.getenv("DB_PATH", "").strip() or str(BASE_DIR / "data" / "agentloop.db"),
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            max_steps=_env_int("MAX_STEPS", 6),
            approval_timeout_seconds=_env_int("APPROVAL_TIMEOUT_SECONDS", 600),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
        )


settings = Settings.from_env()
