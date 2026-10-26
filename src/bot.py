"""Telegram command handlers and application wiring (python-telegram-bot v21, async)."""
from __future__ import annotations

import asyncio
import logging
from functools import wraps

from pymongo.errors import PyMongoError
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from src.config import Settings
from src.database import Database
from src.durations import parse_duration_minutes
from src.permissions import Role, check_admin_change, is_allowed, role_for
from src.promotions import delete_promotion, describe_error, publish_promotion, utcnow
from src.scheduler import run_scheduler

log = logging.getLogger("promo_bot")

HELP = (
    "PROMOTION BOT commands:\n"
    "/addchannel @name or -100ID (owner)\n/removechannel -100ID (owner)\n/channels\n"
    "/addadmin ID, /removeadmin ID, /admins (owner)\n"
    "/publish 24h | 90m | 45h | manual  (reply to the content)\n"
    "/promotions\n/delete P-ID  then  /delete P-ID yes\n"
    "Max automatic lifetime: 45 hours. Manual mode still deletes at 45 hours."
)


def _deps(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Database]:
    return context.application.bot_data["settings"], context.application.bot_data["db"]


def _role(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Role:
    settings, db = _deps(context)
    admin_ids = {d["user_id"] for d in db.admins.find({}, {"_id": 0, "user_id": 1})}
    return role_for(user_id, settings.owner_id, admin_ids)


def requires(minimum: Role):
    def decorator(handler):
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if user is None or not is_allowed(_role(context, user.id), minimum):
                await update.effective_message.reply_text("Not authorized.")
                return
            await handler(update, context)
        return wrapper
    return decorator


def _audit(db: Database, actor: int, action: str, detail: object) -> None:
    db.audit_logs.insert_one({"actor": actor, "action": action, "detail": str(detail), "at": utcnow()})


def _int_arg(args: list[str]) -> int | None:
    try:
        return int(args[0]) if args else None
    except ValueError:
        return None


def _mode_label(minutes: int | None) -> str:
    if minutes is None:
        return "Manual (45h safety deadline applies)"
    return f"Automatic after {minutes // 60}h {minutes % 60}m"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


cmd_help = cmd_start


@requires(Role.OWNER)
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_admin(update, context, add=True)


@requires(Role.OWNER)
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_admin(update, context, add=False)


async def _change_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, add: bool) -> None:
    settings, db = _deps(context)
    target = _int_arg(context.args)
    if target is None:
        await update.effective_message.reply_text("Usage: /addadmin 123456789 or /removeadmin 123456789")
        return
    try:
        check_admin_change(Role.OWNER, target, settings.owner_id)
    except PermissionError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    actor = update.effective_user.id
    if add:
        db.admins.update_one({"user_id": target}, {"$setOnInsert": {"user_id": target, "added_by": actor,
                                                                    "added_at": utcnow()}}, upsert=True)
    else:
        db.admins.delete_one({"user_id": target})
    _audit(db, actor, "add_admin" if add else "remove_admin", target)
    await update.effective_message.reply_text("Done.")


@requires(Role.ADMIN)
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db = _deps(context)
    ids = [str(d["user_id"]) for d in db.admins.find({}, {"_id": 0, "user_id": 1})]
    await update.effective_message.reply_text(f"Owner: {settings.owner_id}\nAdmins: {', '.join(ids) or 'none'}")


@requires(Role.OWNER)
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /addchannel @name or /addchannel -100123456789")
        return
    try:
        chat = await context.bot.get_chat(context.args[0])
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError as exc:
        await update.effective_message.reply_text(describe_error(exc))
        return
    ok = me.status == "creator" or (me.status == "administrator" and getattr(me, "can_post_messages", False))
    if not ok:
        await update.effective_message.reply_text(
            "The bot must be a channel administrator with 'Post messages' and 'Delete messages'.")
        return
    db.channels.update_one({"chat_id": chat.id}, {
        "$set": {"title": chat.title or str(chat.id), "username": chat.username, "enabled": True},
        "$setOnInsert": {"added_by": update.effective_user.id, "added_at": utcnow()}}, upsert=True)
    _audit(db, update.effective_user.id, "add_channel", chat.id)
    await update.effective_message.reply_text(f"Channel added: {chat.title or chat.id}")


@requires(Role.OWNER)
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    chat_id = _int_arg(context.args)
    if chat_id is None:
        await update.effective_message.reply_text("Usage: /removechannel -100123456789")
        return
    db.channels.delete_one({"chat_id": chat_id})
    _audit(db, update.effective_user.id, "remove_channel", chat_id)
    await update.effective_message.reply_text("Channel removed. Existing copies are still tracked for deletion.")


@requires(Role.ADMIN)
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    lines = [f"{'ON ' if c.get('enabled') else 'OFF'} {c.get('title')} ({c['chat_id']})"
             + (f" last error: {c['last_error']}" if c.get("last_error") else "")
             for c in db.channels.find({}, {"_id": 0})]
    await update.effective_message.reply_text("\n".join(lines) or "No channels registered.")


@requires(Role.ADMIN)
async def cmd_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db = _deps(context)
    msg = update.effective_message
    src = msg.reply_to_message
    if src is None or not context.args:
        await msg.reply_text("Reply to the content with: /publish 24h (or 90m, 45h, manual)")
        return
    try:
        minutes = None if context.args[0].lower() == "manual" else parse_duration_minutes(context.args[0])
    except ValueError as exc:
        await msg.reply_text(str(exc))
        return
    channels = [c["chat_id"] for c in db.channels.find({"enabled": True}, {"_id": 0, "chat_id": 1})]
    if not channels:
        await msg.reply_text("No enabled channels. Use /addchannel first.")
        return
    await msg.reply_text(f"Publishing to {len(channels)} channel(s)...")
    try:
        promo_id, ok, failures = await publish_promotion(
            context.bot, db, msg.chat_id, src.message_id, channels, minutes, update.effective_user.id)
    except PyMongoError:
        await msg.reply_text("Database unavailable. Nothing was published.")
        return
    _audit(db, update.effective_user.id, "publish", promo_id)
    lines = [f"Promotion {promo_id}", f"Published: {ok} of {len(channels)}", _mode_label(minutes)]
    lines += [f"Failed {f['chat_id']}: {f['reason']}" for f in failures]
    await msg.reply_text("\n".join(lines))


@requires(Role.ADMIN)
async def cmd_promotions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    lines = []
    for p in db.promotions.find({"status": "active"}, {"_id": 0}).sort("created_at", -1).limit(20):
        nxt = db.promotion_messages.find_one({"promotion_id": p["promotion_id"], "status": "pending"},
                                             {"_id": 0, "delete_at": 1}, sort=[("delete_at", 1)])
        when = f"next deletion {nxt['delete_at']:%Y-%m-%d %H:%M} UTC" if nxt else "no pending deletions"
        lines.append(f"{p['promotion_id']}: {p.get('published', 0)} live, {when}")
    await update.effective_message.reply_text("\n".join(lines) or "No active promotions.")


@requires(Role.ADMIN)
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /delete P-ID, then /delete P-ID yes to confirm.")
        return
    promo_id = context.args[0]
    promo = db.promotions.find_one({"promotion_id": promo_id, "status": {"$ne": "closed"}}, {"_id": 0})
    if promo is None:
        await update.effective_message.reply_text("No open promotion with that ID.")
        return
    pending = db.promotion_messages.count_documents({"promotion_id": promo_id, "status": "pending"})
    if len(context.args) < 2 or context.args[1].lower() != "yes":
        await update.effective_message.reply_text(
            f"This deletes {pending} message(s) from {promo_id}. Send: /delete {promo_id} yes")
        return
    counts = await delete_promotion(context.bot, db, promo_id)
    _audit(db, update.effective_user.id, "manual_delete", promo_id)
    lines = [f"Deleted: {counts['deleted']}", f"Already gone: {counts['already_gone']}",
             f"Retry later: {counts['pending']}"]
    lines += [f"Failed {f['chat_id']}: {f['reason']}" for f in counts["failed"]]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Nothing to cancel.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    # Log only the exception type and a redacted message, never the traceback (it can contain the token URL).
    log.error("Unhandled %s: %s", type(context.error).__name__,
              str(context.error).replace(settings.bot_token, "***")[:300])
    if isinstance(update, Update) and update.effective_message:
        text = ("Database temporarily unavailable. Nothing was changed."
                if isinstance(context.error, PyMongoError) else "Something went wrong. It has been logged.")
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            pass


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]

    async def notify(text: str) -> None:
        try:
            await app.bot.send_message(settings.owner_id, text)
        except TelegramError as exc:
            log.error("Could not notify owner: %s", type(exc).__name__)

    app.bot_data["scheduler_task"] = asyncio.create_task(run_scheduler(app.bot, db, notify))


def build_application(settings: Settings, db: Database) -> Application:
    app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["db"] = db
    commands = {
        "start": cmd_start, "help": cmd_help, "addadmin": cmd_addadmin, "removeadmin": cmd_removeadmin,
        "admins": cmd_admins, "addchannel": cmd_addchannel, "removechannel": cmd_removechannel,
        "channels": cmd_channels, "publish": cmd_publish, "promotions": cmd_promotions,
        "delete": cmd_delete, "cancel": cmd_cancel,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_error_handler(on_error)
    return app
