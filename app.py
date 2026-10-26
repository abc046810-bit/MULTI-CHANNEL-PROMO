"""Entry point: connects to MongoDB and serves Telegram webhooks on Render's PORT."""
import logging

from pymongo.errors import PyMongoError

from src.bot import build_application
from src.config import load_settings
from src.database import Database


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs, which contain the bot token. Keep them out of logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings()
    db = Database(settings.mongodb_uri, settings.db_name)
    try:
        db.ensure_indexes()
    except PyMongoError:
        raise RuntimeError("Cannot reach MongoDB. Check MONGODB_URI and Atlas network access.") from None

    app = build_application(settings, db)
    app.run_webhook(
        listen="0.0.0.0",
        port=settings.port,
        url_path="telegram",
        webhook_url=f"{settings.public_url}/telegram",
        secret_token=settings.webhook_secret,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
