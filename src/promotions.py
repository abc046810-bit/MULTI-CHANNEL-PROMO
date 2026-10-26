"""Publishing and deletion of promotion copies. Every Telegram message gets its own durable record."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from pymongo import ReturnDocument
from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from src.database import Database
from src.durations import deletion_deadline

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=5)
PACING_SECONDS = 0.12  # stay under Telegram flood limits


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def describe_error(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, Forbidden):
        return "Bot lacks rights here (needs admin with post and delete permissions)."
    if "chat not found" in text.lower():
        return "Channel not found. Check the chat ID and that the bot was added."
    return f"Telegram error: {text[:160]}"


async def _with_flood_wait(make_call, attempts: int = 3) -> Any:
    for _ in range(attempts):
        try:
            return await make_call()
        except RetryAfter as exc:
            wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
            await asyncio.sleep(min(seconds + 1, 60))
    raise RetryAfter(60)


async def _copy_to_channel(
    bot: Bot,
    chat_id: int,
    source_chat_id: int,
    message_ids: list[int],
) -> list[int]:
    """Copy one or more messages. Returns list of new message_ids in the target chat."""
    if len(message_ids) == 1:
        sent = await _with_flood_wait(
            partial(
                bot.copy_message,
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=message_ids[0],
            )
        )
        return [sent.message_id]

    # Album / multi-message — copy_messages keeps them grouped when possible
    try:
        result = await _with_flood_wait(
            partial(
                bot.copy_messages,
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_ids=message_ids,
            )
        )
        # result is MessageId list-like
        ids = []
        for item in result:
            mid = getattr(item, "message_id", None)
            if mid is not None:
                ids.append(mid)
        if ids:
            return ids
    except TelegramError:
        pass

    # Fallback: copy one by one
    ids = []
    for mid in message_ids:
        sent = await _with_flood_wait(
            partial(
                bot.copy_message,
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=mid,
            )
        )
        ids.append(sent.message_id)
        await asyncio.sleep(0.05)
    return ids


async def publish_promotion(
    bot: Bot,
    db: Database,
    source_chat_id: int,
    message_ids: list[int],
    channel_ids: list[int],
    mode_minutes: int | None,
    created_by: int,
):
    """Publish to given channels. message_ids can be a single message or an album."""
    if not message_ids:
        raise ValueError("No message IDs to publish.")
    if not channel_ids:
        raise ValueError("No channels selected.")

    db.ping()
    promo_id = f"P-{utcnow():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"
    db.promotions.insert_one(
        {
            "promotion_id": promo_id,
            "status": "publishing",
            "created_by": created_by,
            "created_at": utcnow(),
            "mode_minutes": mode_minutes,
            "targets": len(channel_ids),
            "source_chat_id": source_chat_id,
            "source_message_ids": message_ids,
        }
    )
    published, failures = 0, []
    for chat_id in channel_ids:
        try:
            new_ids = await _copy_to_channel(bot, chat_id, source_chat_id, message_ids)
            sent_at = utcnow()
            for mid in new_ids:
                db.promotion_messages.insert_one(
                    {
                        "promotion_id": promo_id,
                        "chat_id": chat_id,
                        "message_id": mid,
                        "published_at": sent_at,
                        "delete_at": deletion_deadline(sent_at, mode_minutes),
                        "status": "pending",
                        "attempts": 0,
                        "lease_until": None,
                        "last_error": None,
                    }
                )
            db.channels.update_one(
                {"chat_id": chat_id},
                {"$set": {"last_success_at": sent_at, "last_error": None}},
            )
            published += 1
        except TelegramError as exc:
            reason = describe_error(exc)
            failures.append({"chat_id": chat_id, "reason": reason})
            db.channels.update_one({"chat_id": chat_id}, {"$set": {"last_error": reason}})
            log.warning("Publish failed for channel %s: %s", chat_id, reason)
        await asyncio.sleep(PACING_SECONDS)

    db.promotions.update_one(
        {"promotion_id": promo_id},
        {
            "$set": {
                "status": "active" if published else "closed",
                "published": published,
                "failed": failures,
            }
        },
    )
    log.info("Promotion %s: %d published, %d failed", promo_id, published, len(failures))
    return promo_id, published, failures


async def retry_failed_channels(
    bot: Bot,
    db: Database,
    promo_id: str,
) -> tuple[int, list[dict]]:
    """Retry channels that failed on the original publish."""
    promo = db.promotions.find_one({"promotion_id": promo_id})
    if not promo:
        return 0, []
    failed = list(promo.get("failed") or [])
    if not failed:
        return 0, []

    source_chat_id = promo.get("source_chat_id")
    message_ids = promo.get("source_message_ids") or []
    mode_minutes = promo.get("mode_minutes")
    if not source_chat_id or not message_ids:
        return 0, [{"chat_id": 0, "reason": "Source message data missing; cannot retry."}]

    still_failed = []
    recovered = 0
    for item in failed:
        chat_id = item["chat_id"]
        try:
            new_ids = await _copy_to_channel(bot, chat_id, source_chat_id, message_ids)
            sent_at = utcnow()
            for mid in new_ids:
                db.promotion_messages.insert_one(
                    {
                        "promotion_id": promo_id,
                        "chat_id": chat_id,
                        "message_id": mid,
                        "published_at": sent_at,
                        "delete_at": deletion_deadline(sent_at, mode_minutes),
                        "status": "pending",
                        "attempts": 0,
                        "lease_until": None,
                        "last_error": None,
                    }
                )
            db.channels.update_one(
                {"chat_id": chat_id},
                {"$set": {"last_success_at": sent_at, "last_error": None}},
            )
            recovered += 1
        except TelegramError as exc:
            reason = describe_error(exc)
            still_failed.append({"chat_id": chat_id, "reason": reason})
            db.channels.update_one({"chat_id": chat_id}, {"$set": {"last_error": reason}})
        await asyncio.sleep(PACING_SECONDS)

    # Update promotion record
    pub = (promo.get("published") or 0) + recovered
    db.promotions.update_one(
        {"promotion_id": promo_id},
        {
            "$set": {
                "published": pub,
                "failed": still_failed,
                "status": "active" if pub > 0 else promo.get("status", "closed"),
            }
        },
    )
    return recovered, still_failed


def claim_messages(db: Database, query: dict, now: datetime, limit: int) -> list[dict]:
    """Atomically lease pending records so two workers never delete the same message."""
    base = {"status": "pending", "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}]}
    claimed: list[dict] = []
    for _ in range(limit):
        doc = db.promotion_messages.find_one_and_update(
            {**query, **base},
            {"$set": {"lease_until": now + LEASE}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            break
        claimed.append(doc)
    return claimed


async def delete_one_message(bot: Bot, rec: dict) -> tuple[str, str | None]:
    try:
        await _with_flood_wait(
            partial(bot.delete_message, chat_id=rec["chat_id"], message_id=rec["message_id"])
        )
        return "deleted", None
    except BadRequest as exc:
        if "not found" in str(exc).lower():
            return "already_gone", None
        return "failed", describe_error(exc)
    except Forbidden as exc:
        return "failed", describe_error(exc)
    except TelegramError as exc:
        return "retry", describe_error(exc)


def record_delete_result(db: Database, rec: dict, status: str, error: str | None) -> str:
    attempts = rec.get("attempts", 0) + 1
    final = status
    if status == "retry":
        final = "pending" if attempts < MAX_ATTEMPTS else "failed"
    finished = final in ("deleted", "already_gone")
    db.promotion_messages.update_one(
        {"_id": rec["_id"]},
        {
            "$set": {
                "status": final,
                "attempts": attempts,
                "lease_until": None,
                "last_error": error,
                "deleted_at": utcnow() if finished else None,
            }
        },
    )
    return final


def close_if_done(db: Database, promo_id: str) -> None:
    if db.promotion_messages.count_documents({"promotion_id": promo_id, "status": "pending"}) == 0:
        db.promotions.update_one({"promotion_id": promo_id}, {"$set": {"status": "closed"}})


async def delete_promotion(bot: Bot, db: Database, promo_id: str) -> dict:
    """Manual deletion of one promotion across all its channels."""
    counts: dict = {"deleted": 0, "already_gone": 0, "pending": 0, "failed": []}
    now = utcnow()
    for rec in claim_messages(db, {"promotion_id": promo_id}, now, limit=500):
        status, error = await delete_one_message(bot, rec)
        final = record_delete_result(db, rec, status, error)
        if final in counts and isinstance(counts[final], int):
            counts[final] += 1
        if final == "failed":
            counts["failed"].append({"chat_id": rec["chat_id"], "reason": error})
        await asyncio.sleep(PACING_SECONDS)
    close_if_done(db, promo_id)
    return counts


async def delete_many_promotions(bot: Bot, db: Database, promo_ids: list[str]) -> dict:
    """Delete multiple promotions. Returns aggregate counts."""
    total = {"deleted": 0, "already_gone": 0, "pending": 0, "failed": [], "promos": 0}
    for pid in promo_ids:
        c = await delete_promotion(bot, db, pid)
        total["deleted"] += c["deleted"]
        total["already_gone"] += c["already_gone"]
        total["pending"] += c["pending"]
        total["failed"].extend(c["failed"])
        total["promos"] += 1
    return total
