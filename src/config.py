"""Environment configuration. Secrets come only from environment variables."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass


# Telegram setWebhook secret_token: 1-256 chars, only A-Za-z0-9_-
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongodb_uri: str
    db_name: str
    owner_id: int
    webhook_secret: str
    public_url: str
    port: int


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _sanitize_webhook_secret(raw: str) -> str:
    """Make any Render-generated (or user-supplied) secret valid for Telegram.

    Telegram rejects secret_token values that contain characters outside
    [A-Za-z0-9_-]. Render's generateValue often includes other characters.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw)
    if len(cleaned) >= 16 and _SECRET_RE.match(cleaned):
        return cleaned
    # Fallback: generate a safe secret ourselves
    return secrets.token_urlsafe(32).replace("=", "").replace("+", "").replace("/", "")[:64]


def load_settings() -> Settings:
    raw_owner = _required("OWNER_ID")
    if not raw_owner.isdigit():
        raise RuntimeError("OWNER_ID must be a numeric Telegram user ID.")
    public_url = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    if not public_url:
        raise RuntimeError("Set PUBLIC_URL (Render normally sets RENDER_EXTERNAL_URL automatically).")

    # WEBHOOK_SECRET is optional at the env level; we always end up with a valid one.
    raw_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    webhook_secret = _sanitize_webhook_secret(raw_secret) if raw_secret else _sanitize_webhook_secret("")

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        mongodb_uri=_required("MONGODB_URI"),
        db_name=os.environ.get("MONGODB_DB", "promo_bot"),
        owner_id=int(raw_owner),
        webhook_secret=webhook_secret,
        public_url=public_url.rstrip("/"),
        port=int(os.environ.get("PORT", "10000")),
    )
    
