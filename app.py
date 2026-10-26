"""Entry point: MongoDB + Telegram webhook + health endpoint for UptimeRobot."""
from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web
from pymongo.errors import PyMongoError
from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError

from src.bot import build_application
from src.config import load_settings
from src.database import Database


async def _run(settings, db, log) -> None:
    application = build_application(settings, db)
    await application.initialize()
    await application.start()

    # Register webhook with Telegram
    await application.bot.set_webhook(
        url=f"{settings.public_url}/telegram",
        secret_token=settings.webhook_secret,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    log.info("Webhook set to %s/telegram", settings.public_url)

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/plain")

    async def telegram_webhook(request: web.Request) -> web.Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != settings.webhook_secret:
            return web.Response(status=403, text="forbidden")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        update = Update.de_json(data, application.bot)
        if update:
            await application.process_update(update)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/telegram", telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    log.info("Listening on 0.0.0.0:%s (GET / and /health, POST /telegram)", settings.port)

    # Block forever
    await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("promo_bot")

    settings = load_settings()
    db = Database(settings.mongodb_uri, settings.db_name)
    try:
        db.ensure_indexes()
    except PyMongoError:
        raise RuntimeError(
            "Cannot reach MongoDB. Check MONGODB_URI, the password, and Atlas Network Access."
        ) from None

    attempts = 5
    for attempt in range(1, attempts + 1):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run(settings, db, log))
            return
        except BadRequest as exc:
            msg = str(exc)
            log.error("Startup attempt %d/%d failed: BadRequest – %s", attempt, attempts, msg[:200])
            if "secret token" in msg.lower() or "illegal characters" in msg.lower():
                log.error(
                    "WEBHOOK_SECRET rejected by Telegram. Delete WEBHOOK_SECRET in Render "
                    "or set only A-Za-z0-9_- characters."
                )
            if attempt == attempts:
                raise
            time.sleep(min(10 * attempt, 60))
        except NetworkError as exc:
            log.error("Startup attempt %d/%d failed: %s", attempt, attempts, type(exc).__name__)
            if attempt == attempts:
                raise
            time.sleep(min(10 * attempt, 60))
        except TelegramError as exc:
            log.error(
                "Startup attempt %d/%d failed: %s – %s",
                attempt, attempts, type(exc).__name__, str(exc)[:200],
            )
            if attempt == attempts:
                raise
            time.sleep(min(10 * attempt, 60))
        finally:
            if not loop.is_closed():
                loop.close()


if __name__ == "__main__":
    main()
