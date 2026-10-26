"""MongoDB-backed deletion scheduler. State lives in the database, so restarts resume overdue work."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.database import Database
from src.promotions import claim_messages, close_if_done, delete_one_message, record_delete_result, utcnow

log = logging.getLogger(__name__)


async def run_due_once(bot, db: Database, notify: Callable[[str], Awaitable[None]], batch: int = 25) -> None:
    now = utcnow()
    for rec in claim_messages(db, {"delete_at": {"$lte": now}}, now, batch):
        status, error = await delete_one_message(bot, rec)
        final = record_delete_result(db, rec, status, error)
        if final == "failed":
            await notify(f"Could not delete {rec['promotion_id']} from chat {rec['chat_id']}: {error}")
        close_if_done(db, rec["promotion_id"])


async def run_scheduler(bot, db: Database, notify: Callable[[str], Awaitable[None]], interval: int = 15) -> None:
    log.info("Deletion scheduler started (interval %ss, IST deadlines)", interval)
    while True:
        try:
            await run_due_once(bot, db, notify)
        except Exception as exc:  # keep the loop alive; never log secrets
            log.error("Scheduler tick failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)
        
