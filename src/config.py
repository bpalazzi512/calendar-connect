"""Everything the function reads from its environment.

Terraform sets all of these; README.md says what each one does.
"""

from __future__ import annotations

import os
from typing import Any


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def load() -> dict[str, Any]:
    """The whole configuration. Raises RuntimeError if something's missing."""
    return {
        "bot_token": _env("TELEGRAM_BOT_TOKEN", required=True),
        "webhook_secret": _env("TELEGRAM_WEBHOOK_SECRET", required=True),
        "allowed_user_id": _env("ALLOWED_TELEGRAM_USER_ID", required=True),
        "llm_base_url": _env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "llm_model": _env("LLM_MODEL", "deepseek-v4-flash"),
        "llm_api_key": _env("LLM_API_KEY", required=True),
        "llm_timeout": int(_env("LLM_TIMEOUT_SECONDS", "25")),
        "calendar_id": _env("CALENDAR_ID", required=True),
        "timezone": _env("TIMEZONE", "America/New_York"),
        "default_minutes": int(_env("DEFAULT_EVENT_MINUTES", "60")),
        # Not required. When it's unset the JSON API refuses every request
        # rather than serving them unauthenticated; Telegram is unaffected.
        "api_token": _env("API_TOKEN"),
    }
