"""Entry point: connects to MongoDB and serves Telegram webhooks on Render's PORT."""
import asyncio
import logging
import time

from pymongo.errors import PyMongoError
from telegram.error import NetworkError

from src.bot import build_application
from src.config import load_settings
from src.database import Database


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs, which contain the bot token. Keep them out of logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("promo_bot")

    settings = load_settings()
    db = Database(settings.mongodb_uri, settings.db_name)
    try:
        db.ensure_indexes()
    except PyMongoError:
        raise RuntimeError("Cannot reach MongoDB. Check MONGODB_URI, the password, and Atlas Network Access.") from None

    attempts = 5
    for attempt in range(1, attempts + 1):
        # python-telegram-bot 21 calls asyncio.get_event_loop(); give it a fresh loop every attempt.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = build_application(settings, db)
        try:
            app.run_webhook(
                listen="0.0.0.0",
                port=settings.port,
                url_path="telegram",
                webhook_url=f"{settings.public_url}/telegram",
                secret_token=settings.webhook_secret,
                drop_pending_updates=True,
            )
            return
        except NetworkError as exc:
            # Startup calls to api.telegram.org (get_me, set_webhook) can time out on cold starts.
            log.error("Startup attempt %d/%d failed: %s", attempt, attempts, type(exc).__name__)
            if attempt == attempts:
                raise
            time.sleep(min(10 * attempt, 60))
        finally:
            if not loop.is_closed():
                loop.close()


if __name__ == "__main__":
    main()
