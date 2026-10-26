"""Environment configuration. Secrets come only from environment variables."""
import os
from dataclasses import dataclass


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


def load_settings() -> Settings:
    raw_owner = _required("OWNER_ID")
    if not raw_owner.isdigit():
        raise RuntimeError("OWNER_ID must be a numeric Telegram user ID.")
    public_url = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    if not public_url:
        raise RuntimeError("Set PUBLIC_URL (Render normally sets RENDER_EXTERNAL_URL automatically).")
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        mongodb_uri=_required("MONGODB_URI"),
        db_name=os.environ.get("MONGODB_DB", "promo_bot"),
        owner_id=int(raw_owner),
        webhook_secret=_required("WEBHOOK_SECRET"),
        public_url=public_url.rstrip("/"),
        port=int(os.environ.get("PORT", "10000")),
    )
