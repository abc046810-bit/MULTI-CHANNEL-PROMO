"""Premium Telegram Multi-Channel Promotion Bot — full admin panel + solid backend."""
from __future__ import annotations

import asyncio
import logging
from functools import wraps

from pymongo.errors import PyMongoError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from src.config import Settings
from src.database import Database
from src.durations import DURATION_PRESETS, format_duration, parse_duration_minutes
from src.permissions import Role, check_admin_change, is_allowed, role_for
from src.promotions import delete_promotion, describe_error, publish_promotion, utcnow
from src.scheduler import run_scheduler

log = logging.getLogger("promo_bot")

# ─── Bot menu (Telegram side menu) ──────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start", "Open premium panel"),
    BotCommand("help", "How to use the bot"),
    BotCommand("publish", "Publish (reply + time)"),
    BotCommand("promotions", "Active promotions"),
    BotCommand("channels", "Manage channels"),
    BotCommand("stats", "Dashboard statistics"),
    BotCommand("admins", "Admin list"),
    BotCommand("cancel", "Cancel current action"),
]

# ─── Keyboards ──────────────────────────────────────────────────────────────


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def main_menu_kb(role: Role) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📢 New Promotion", callback_data="panel:publish_help")],
        [
            InlineKeyboardButton("📡 Channels", callback_data="panel:channels"),
            InlineKeyboardButton("📋 Promotions", callback_data="panel:promotions"),
        ],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="panel:stats"),
            InlineKeyboardButton("👥 Admins", callback_data="panel:admins"),
        ],
        [InlineKeyboardButton("❓ Help", callback_data="panel:help")],
    ]
    if role >= Role.OWNER:
        rows.insert(3, [
            InlineKeyboardButton("➕ Add Channel", callback_data="panel:addchannel_help"),
            InlineKeyboardButton("🛡️ Manage Admins", callback_data="panel:manage_admins"),
        ])
    return _kb(rows)


def duration_kb() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for label, minutes in DURATION_PRESETS:
        key = "manual" if minutes is None else str(minutes)
        row.append(InlineKeyboardButton(label, callback_data=f"dur:{key}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="panel:home")])
    return _kb(buttons)


def back_home_kb() -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")]])


def confirm_delete_kb(promo_id: str) -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delconfirm:{promo_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="panel:promotions"),
        ]
    ])


# ─── Text blocks ─────────────────────────────────────────────────────────────

WELCOME = (
    "✨ <b>Multi-Channel Promo Bot</b>\n"
    "────────────────────────\n"
    "Sabhi channels pe ek saath promotion bhejein.\n"
    "Time poora hone par automatic delete.\n\n"
    "⏰ Max auto-delete: <b>45 hours</b>\n"
    "────────────────────────\n"
    "Neeche se panel kholo 👇"
)

HELP = (
    "📖 <b>Kaise use karein</b>\n"
    "────────────────────────\n\n"
    "<b>1. Channel jodein</b> (Owner)\n"
    "Bot ko channel mein Admin banao\n"
    "(Post + Delete permission)\n"
    "Phir: <code>/addchannel @name</code>\n\n"
    "<b>2. Promotion bhejein</b>\n"
    "• Content pe <b>Reply</b> karo\n"
    "• <code>/publish 24h</code> bhejo\n"
    "  ya button se time choose karo\n\n"
    "<b>3. Auto-delete</b>\n"
    "Bot time ke baad khud delete karega.\n"
    "Manual mode mein bhi 45h safety hai.\n\n"
    "<b>4. Jaldi delete</b>\n"
    "<code>/delete P-ID</code> phir confirm.\n\n"
    "────────────────────────\n"
    "💡 Side menu (message box ke paas) se bhi commands milenge."
)

PUBLISH_HELP = (
    "📢 <b>New Promotion</b>\n"
    "────────────────────────\n"
    "1. Jo post bhejni hai uspe <b>Reply</b> karo\n"
    "2. Phir <code>/publish</code> likho\n"
    "3. Time choose karo (buttons se ya type karke)\n\n"
    "Examples:\n"
    "<code>/publish 24h</code>\n"
    "<code>/publish 90m</code>\n"
    "<code>/publish 45h</code>\n"
    "<code>/publish manual</code>\n\n"
    "⏰ Maximum <b>45 hours</b>."
)


# ─── Helpers ────────────────────────────────────────────────────────────────

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
                text = "🔒 Aapke paas is action ka access nahi hai."
                if update.callback_query:
                    await update.callback_query.answer(text, show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text(text)
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


async def _edit_or_reply(update: Update, text: str, markup=None) -> None:
    """Prefer editing callback message; otherwise reply."""
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
            )
            return
        except TelegramError:
            pass
    if update.effective_message:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )


# ─── Panel screens ──────────────────────────────────────────────────────────

async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = _role(context, update.effective_user.id)
    await _edit_or_reply(update, WELCOME, main_menu_kb(role))


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _edit_or_reply(update, HELP, back_home_kb())


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    s = db.stats()
    text = (
        "📊 <b>Dashboard</b>\n"
        "────────────────────────\n"
        f"📡 Channels: <b>{s['channels_enabled']}</b> / {s['channels_total']} enabled\n"
        f"👥 Admins: <b>{s['admins']}</b>\n"
        f"📋 Active promotions: <b>{s['promotions_active']}</b>\n"
        f"📦 Total promotions: <b>{s['promotions_total']}</b>\n"
        f"⏳ Pending deletes: <b>{s['messages_pending']}</b>\n"
        f"✅ Deleted messages: <b>{s['messages_deleted']}</b>\n"
        f"❌ Failed deletes: <b>{s['messages_failed']}</b>"
    )
    await _edit_or_reply(update, text, back_home_kb())


async def show_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    channels = list(db.channels.find({}, {"_id": 0}).sort("title", 1))
    if not channels:
        text = (
            "📭 <b>No channels yet</b>\n"
            "────────────────────────\n"
            "Owner /addchannel se channel jod sakte hain."
        )
        await _edit_or_reply(update, text, back_home_kb())
        return

    lines = ["📡 <b>Channels</b>\n────────────────────────"]
    buttons: list[list[InlineKeyboardButton]] = []
    for c in channels[:30]:  # safety cap for Telegram message size
        status = "🟢" if c.get("enabled") else "🔴"
        title = (c.get("title") or "Unknown")[:28]
        err = f"\n   ⚠️ {c['last_error'][:60]}" if c.get("last_error") else ""
        lines.append(f"{status} <b>{title}</b>\n   <code>{c['chat_id']}</code>{err}")
        # Toggle button
        toggle_label = "Disable" if c.get("enabled") else "Enable"
        buttons.append([
            InlineKeyboardButton(
                f"{toggle_label} · {title[:18]}",
                callback_data=f"chtoggle:{c['chat_id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")])
    await _edit_or_reply(update, "\n\n".join(lines), _kb(buttons))


async def show_promotions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    promos = list(db.promotions.find({"status": "active"}, {"_id": 0}).sort("created_at", -1).limit(15))
    if not promos:
        await _edit_or_reply(
            update,
            "📋 <b>Active Promotions</b>\n────────────────────────\nAbhi koi active promotion nahi hai.",
            back_home_kb(),
        )
        return

    lines = ["📋 <b>Active Promotions</b>\n────────────────────────"]
    buttons: list[list[InlineKeyboardButton]] = []
    for p in promos:
        pid = p["promotion_id"]
        nxt = db.promotion_messages.find_one(
            {"promotion_id": pid, "status": "pending"},
            {"_id": 0, "delete_at": 1},
            sort=[("delete_at", 1)],
        )
        when = f"⏰ {nxt['delete_at']:%d %b %H:%M} UTC" if nxt else "✅ No pending"
        mode = format_duration(p.get("mode_minutes"))
        lines.append(
            f"<b>{pid}</b>\n"
            f"   {p.get('published', 0)} live · {mode}\n"
            f"   {when}"
        )
        buttons.append([InlineKeyboardButton(f"🗑 Delete {pid}", callback_data=f"delask:{pid}")])
    buttons.append([InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")])
    await _edit_or_reply(update, "\n\n".join(lines), _kb(buttons))


async def show_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, db = _deps(context)
    ids = [str(d["user_id"]) for d in db.admins.find({}, {"_id": 0, "user_id": 1})]
    text = (
        "👥 <b>Administrators</b>\n"
        "────────────────────────\n"
        f"👑 Owner: <code>{settings.owner_id}</code>\n"
        f"🛡️ Admins: {', '.join(f'<code>{i}</code>' for i in ids) or 'None'}"
    )
    role = _role(context, update.effective_user.id)
    rows = [[InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")]]
    if role >= Role.OWNER:
        rows.insert(0, [InlineKeyboardButton("➕ Add / Remove Admin help", callback_data="panel:manage_admins")])
    await _edit_or_reply(update, text, _kb(rows))


# ─── Commands ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await show_home(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_help(update, context)


@requires(Role.ADMIN)
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_stats(update, context)


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
        await update.effective_message.reply_text(
            "👉 Usage:\n<code>/addadmin 123456789</code>\n<code>/removeadmin 123456789</code>\n\n"
            "User ID @userinfobot se milta hai.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        check_admin_change(Role.OWNER, target, settings.owner_id)
    except PermissionError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
        return
    actor = update.effective_user.id
    if add:
        db.admins.update_one(
            {"user_id": target},
            {"$setOnInsert": {"user_id": target, "added_by": actor, "added_at": utcnow()}},
            upsert=True,
        )
        await update.effective_message.reply_text(
            f"✅ Admin added: <code>{target}</code>", parse_mode=ParseMode.HTML
        )
    else:
        db.admins.delete_one({"user_id": target})
        await update.effective_message.reply_text(
            f"✅ Admin removed: <code>{target}</code>", parse_mode=ParseMode.HTML
        )
    _audit(db, actor, "add_admin" if add else "remove_admin", target)


@requires(Role.ADMIN)
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_admins(update, context)


@requires(Role.OWNER)
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text(
            "👉 <b>Channel kaise jodein</b>\n"
            "1. Bot ko channel mein <b>Admin</b> banao\n"
            "   (Post messages + Delete messages)\n"
            "2. Phir bhejo:\n"
            "<code>/addchannel @channelname</code>\n"
            "ya <code>/addchannel -1001234567890</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        chat = await context.bot.get_chat(context.args[0])
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError as exc:
        await update.effective_message.reply_text(f"⚠️ {describe_error(exc)}")
        return
    ok = me.status == "creator" or (
        me.status == "administrator" and getattr(me, "can_post_messages", False)
    )
    if not ok:
        await update.effective_message.reply_text(
            "⚠️ Bot ko channel mein Admin banao aur\n"
            "<b>Post messages</b> + <b>Delete messages</b> do.",
            parse_mode=ParseMode.HTML,
        )
        return
    db.channels.update_one(
        {"chat_id": chat.id},
        {
            "$set": {
                "title": chat.title or str(chat.id),
                "username": chat.username,
                "enabled": True,
            },
            "$setOnInsert": {"added_by": update.effective_user.id, "added_at": utcnow()},
        },
        upsert=True,
    )
    _audit(db, update.effective_user.id, "add_channel", chat.id)
    await update.effective_message.reply_text(
        f"✅ Channel added!\n\n📢 <b>{chat.title or chat.id}</b>\n<code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


@requires(Role.OWNER)
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    chat_id = _int_arg(context.args)
    if chat_id is None:
        await update.effective_message.reply_text(
            "👉 <code>/removechannel -1001234567890</code>\n"
            "Channel ID /channels se milta hai.",
            parse_mode=ParseMode.HTML,
        )
        return
    db.channels.delete_one({"chat_id": chat_id})
    _audit(db, update.effective_user.id, "remove_channel", chat_id)
    await update.effective_message.reply_text(
        "✅ Channel removed. Already published copies ab bhi track hongi."
    )


@requires(Role.ADMIN)
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_channels(update, context)


@requires(Role.ADMIN)
async def cmd_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Classic command path: reply + /publish 24h  OR  reply + /publish then pick duration."""
    settings, db = _deps(context)
    msg = update.effective_message
    src = msg.reply_to_message

    if src is None:
        await msg.reply_text(PUBLISH_HELP, parse_mode=ParseMode.HTML, reply_markup=back_home_kb())
        return

    # Store source for button-based duration selection
    context.user_data["publish_src"] = {
        "chat_id": msg.chat_id,
        "message_id": src.message_id,
    }

    if not context.args:
        # Ask for duration with premium buttons
        await msg.reply_text(
            "⏰ <b>Delete time choose karo</b>\n"
            "────────────────────────\n"
            "Promotion kitni der baad delete ho?",
            parse_mode=ParseMode.HTML,
            reply_markup=duration_kb(),
        )
        return

    # Direct duration provided
    try:
        minutes = None if context.args[0].lower() == "manual" else parse_duration_minutes(context.args[0])
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}")
        return
    await _do_publish(update, context, msg.chat_id, src.message_id, minutes)


async def _do_publish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    source_message_id: int,
    minutes: int | None,
) -> None:
    settings, db = _deps(context)
    channels = [c["chat_id"] for c in db.channels.find({"enabled": True}, {"_id": 0, "chat_id": 1})]
    if not channels:
        text = "⚠️ Koi enabled channel nahi hai.\nPehle channel add + enable karo."
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
        return

    status_msg = None
    if update.effective_message:
        status_msg = await update.effective_message.reply_text(
            f"🚀 Publishing to <b>{len(channels)}</b> channel(s)...",
            parse_mode=ParseMode.HTML,
        )
    elif update.callback_query and update.callback_query.message:
        status_msg = await update.callback_query.message.reply_text(
            f"🚀 Publishing to <b>{len(channels)}</b> channel(s)...",
            parse_mode=ParseMode.HTML,
        )

    try:
        promo_id, ok, failures = await publish_promotion(
            context.bot,
            db,
            source_chat_id,
            source_message_id,
            channels,
            minutes,
            update.effective_user.id,
        )
    except PyMongoError:
        if status_msg:
            await status_msg.edit_text("⚠️ Database unavailable. Kuch publish nahi hua.")
        return

    _audit(db, update.effective_user.id, "publish", promo_id)
    lines = [
        f"✅ <b>Promotion {promo_id}</b>",
        f"Published: <b>{ok}</b> / {len(channels)}",
        f"Mode: {format_duration(minutes)}",
    ]
    for f in failures:
        lines.append(f"❌ <code>{f['chat_id']}</code>: {f['reason']}")
    text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            await status_msg.reply_text(text, parse_mode=ParseMode.HTML)
    context.user_data.pop("publish_src", None)


@requires(Role.ADMIN)
async def cmd_promotions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_promotions(update, context)


@requires(Role.ADMIN)
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text(
            "👉 <code>/delete P-20261026-AB12</code>\n"
            "Phir confirm: <code>/delete P-... yes</code>\n\n"
            "Ya panel se 🗑 button use karo.",
            parse_mode=ParseMode.HTML,
        )
        return
    promo_id = context.args[0]
    promo = db.promotions.find_one({"promotion_id": promo_id, "status": {"$ne": "closed"}}, {"_id": 0})
    if promo is None:
        await update.effective_message.reply_text("⚠️ Is ID ka open promotion nahi mila.")
        return
    pending = db.promotion_messages.count_documents({"promotion_id": promo_id, "status": "pending"})
    if len(context.args) < 2 or context.args[1].lower() != "yes":
        await update.effective_message.reply_text(
            f"⚠️ Yeh <b>{pending}</b> message(s) delete karega.\n"
            f"Confirm: <code>/delete {promo_id} yes</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_delete_kb(promo_id),
        )
        return
    await _run_delete(update, context, promo_id)


async def _run_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, promo_id: str) -> None:
    _, db = _deps(context)
    if update.callback_query:
        await update.callback_query.answer("Deleting...")
    counts = await delete_promotion(context.bot, db, promo_id)
    _audit(db, update.effective_user.id, "manual_delete", promo_id)
    lines = [
        f"🗑 <b>{promo_id}</b>",
        f"✅ Deleted: {counts['deleted']}",
        f"ℹ️ Already gone: {counts['already_gone']}",
        f"⏳ Retry later: {counts['pending']}",
    ]
    for f in counts["failed"]:
        lines.append(f"❌ {f['chat_id']}: {f['reason']}")
    await _edit_or_reply(update, "\n".join(lines), back_home_kb())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text("✅ Cancelled.", reply_markup=back_home_kb())


# ─── Callback router ────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user = update.effective_user
    if user is None:
        return
    role = _role(context, user.id)

    # ── Panel navigation
    if data == "panel:home":
        await show_home(update, context)
        return
    if data == "panel:help":
        await show_help(update, context)
        return
    if data == "panel:publish_help":
        await _edit_or_reply(update, PUBLISH_HELP, back_home_kb())
        return
    if data == "panel:stats":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_stats(update, context)
        return
    if data == "panel:channels":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_channels(update, context)
        return
    if data == "panel:promotions":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_promotions(update, context)
        return
    if data == "panel:admins":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_admins(update, context)
        return
    if data == "panel:addchannel_help":
        if not is_allowed(role, Role.OWNER):
            await query.answer("🔒 Owner only", show_alert=True)
            return
        await _edit_or_reply(
            update,
            "➕ <b>Add Channel</b>\n"
            "────────────────────────\n"
            "1. Bot ko channel mein Admin banao\n"
            "2. Command bhejo:\n"
            "<code>/addchannel @name</code>\n"
            "ya <code>/addchannel -100...</code>",
            back_home_kb(),
        )
        return
    if data == "panel:manage_admins":
        if not is_allowed(role, Role.OWNER):
            await query.answer("🔒 Owner only", show_alert=True)
            return
        await _edit_or_reply(
            update,
            "🛡️ <b>Manage Admins</b>\n"
            "────────────────────────\n"
            "<code>/addadmin 123456789</code>\n"
            "<code>/removeadmin 123456789</code>\n\n"
            "User ID @userinfobot se lo.",
            back_home_kb(),
        )
        return

    # ── Duration selected after /publish (reply)
    if data.startswith("dur:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        src = context.user_data.get("publish_src")
        if not src:
            await query.answer("Pehle kisi message pe Reply karke /publish bhejo.", show_alert=True)
            return
        key = data.split(":", 1)[1]
        minutes: int | None
        if key == "manual":
            minutes = None
        else:
            try:
                minutes = int(key)
            except ValueError:
                await query.answer("Invalid duration", show_alert=True)
                return
        await _do_publish(update, context, src["chat_id"], src["message_id"], minutes)
        return

    # ── Channel toggle enable/disable
    if data.startswith("chtoggle:"):
        if not is_allowed(role, Role.OWNER):
            await query.answer("🔒 Owner only", show_alert=True)
            return
        try:
            chat_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        _, db = _deps(context)
        ch = db.channels.find_one({"chat_id": chat_id})
        if not ch:
            await query.answer("Channel not found", show_alert=True)
            return
        new_val = not ch.get("enabled", True)
        db.channels.update_one({"chat_id": chat_id}, {"$set": {"enabled": new_val}})
        _audit(db, user.id, "toggle_channel", f"{chat_id}:{new_val}")
        await show_channels(update, context)
        return

    # ── Delete ask / confirm
    if data.startswith("delask:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        promo_id = data.split(":", 1)[1]
        _, db = _deps(context)
        pending = db.promotion_messages.count_documents({"promotion_id": promo_id, "status": "pending"})
        await _edit_or_reply(
            update,
            f"⚠️ Delete <b>{promo_id}</b>?\n"
            f"Yeh <b>{pending}</b> message(s) sab channels se hata dega.",
            confirm_delete_kb(promo_id),
        )
        return

    if data.startswith("delconfirm:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        promo_id = data.split(":", 1)[1]
        await _run_delete(update, context, promo_id)
        return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    log.error(
        "Unhandled %s: %s",
        type(context.error).__name__,
        str(context.error).replace(settings.bot_token, "***")[:300],
    )
    if isinstance(update, Update) and update.effective_message:
        text = (
            "⚠️ Database temporarily unavailable."
            if isinstance(context.error, PyMongoError)
            else "⚠️ Something went wrong. Error has been logged."
        )
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            pass


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]

    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("Bot commands menu registered")
    except TelegramError as exc:
        log.error("Could not set bot commands: %s", type(exc).__name__)

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
        "start": cmd_start,
        "help": cmd_help,
        "stats": cmd_stats,
        "addadmin": cmd_addadmin,
        "removeadmin": cmd_removeadmin,
        "admins": cmd_admins,
        "addchannel": cmd_addchannel,
        "removechannel": cmd_removechannel,
        "channels": cmd_channels,
        "publish": cmd_publish,
        "promotions": cmd_promotions,
        "delete": cmd_delete,
        "cancel": cmd_cancel,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app
