"""Premium Multi-Channel Promo Bot — full panel, multi-select, confirm, history, retry."""
from __future__ import annotations

import asyncio
import logging
from functools import wraps

from pymongo.errors import PyMongoError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes

from src.config import Settings
from src.database import Database
from src.durations import DURATION_PRESETS, format_duration, format_ist, parse_duration_minutes
from src.permissions import Role, check_admin_change, is_allowed, role_for
from src.promotions import (
    MAX_CHANNELS,
    delete_many_promotions,
    delete_promotion,
    describe_error,
    publish_promotion,
    retry_failed_channels,
    utcnow,
)
from src.scheduler import run_scheduler

log = logging.getLogger("promo_bot")

PAGE_SIZE = 12
PUB_CH_PAGE = 25  # channels per page in publish select (supports 100)

BOT_COMMANDS = [
    BotCommand("start", "Open premium panel"),
    BotCommand("help", "How to use"),
    BotCommand("publish", "Publish (reply)"),
    BotCommand("promotions", "Active promotions"),
    BotCommand("channels", "Manage channels"),
    BotCommand("stats", "Dashboard"),
    BotCommand("admins", "Admin list"),
    BotCommand("cancel", "Cancel action"),
]


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def main_menu_kb(role: Role) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📢 New Promotion", callback_data="panel:publish_help")],
        [
            InlineKeyboardButton("📡 Channels", callback_data="ch:list:0"),
            InlineKeyboardButton("📋 Active", callback_data="pr:list:0"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="panel:stats"),
        ],
        [
            InlineKeyboardButton("👥 Admins", callback_data="panel:admins"),
            InlineKeyboardButton("❓ Help", callback_data="panel:help"),
        ],
    ]
    if role >= Role.OWNER:
        rows.insert(3, [
            InlineKeyboardButton("➕ Add Channel", callback_data="panel:addchannel_help"),
            InlineKeyboardButton("🛡️ Manage Admins", callback_data="panel:manage_admins"),
        ])
    return _kb(rows)


def back_home_kb() -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")]])


def duration_kb() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for label, minutes in DURATION_PRESETS:
        key = "manual" if minutes is None else str(minutes)
        row.append(InlineKeyboardButton(label, callback_data=f"pub:dur:{key}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("◀️ Channels", callback_data="pub:channels"),
        InlineKeyboardButton("❌ Cancel", callback_data="panel:home"),
    ])
    return _kb(buttons)


WELCOME = (
    "✨ <b>Multi-Channel Promo Bot</b>\n"
    "────────────────────────\n"
    "Sabhi / selected channels pe promotion.\n"
    "Auto-delete + manual control.\n\n"
    "⏰ Max auto-delete: <b>45 hours</b>\n"
    "────────────────────────"
)

HELP = (
    "📖 <b>Guide</b>\n"
    "────────────────────────\n\n"
    "<b>1. Channel</b> (Owner)\n"
    "Bot ko Admin banao → /addchannel @name\n\n"
    "<b>2. Publish</b>\n"
    "Message pe <b>Reply</b> → /publish\n"
    "→ Channels → Copy/Forward → Time → Confirm\n\n"
    "<b>3. Auto-delete</b>\n"
    "Time ke baad khud delete. Manual = 45h safety.\n\n"
    "<b>4. Delete / Retry</b>\n"
    "Active list se button, ya /delete P-ID\n\n"
    "<b>Commands</b> (tap to use):\n"
    "/start /help /publish /promotions\n"
    "/channels /stats /admins\n"
    "/addchannel /removechannel\n"
    "/addadmin /removeadmin /delete /cancel\n"
    "/setwelcome  <i>(owner – guest message)</i>\n\n"
    "💡 Side menu se bhi commands milenge."
)

PUBLISH_HELP = (
    "📢 <b>New Promotion</b>\n"
    "────────────────────────\n"
    "1. Content (ya album) pe <b>Reply</b> karo\n"
    "2. <code>/publish</code> bhejo\n"
    "3. Channels choose karo\n"
    "4. Time choose karo\n"
    "5. Preview dekho → Confirm\n\n"
    "Direct bhi chalega:\n"
    "<code>/publish 24h</code> (sab enabled channels)"
)


def _deps(context):
    return context.application.bot_data["settings"], context.application.bot_data["db"]


def _role(context, user_id: int) -> Role:
    settings, db = _deps(context)
    admin_ids = {d["user_id"] for d in db.admins.find({}, {"_id": 0, "user_id": 1})}
    return role_for(user_id, settings.owner_id, admin_ids)


def requires(minimum: Role):
    def decorator(handler):
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if user is None or not is_allowed(_role(context, user.id), minimum):
                text = "🔒 Access nahi hai."
                if update.callback_query:
                    await update.callback_query.answer(text, show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text(text)
                return
            await handler(update, context)
        return wrapper
    return decorator


def _audit(db, actor: int, action: str, detail) -> None:
    db.audit_logs.insert_one({"actor": actor, "action": action, "detail": str(detail), "at": utcnow()})


def _int_arg(args):
    try:
        return int(args[0]) if args else None
    except ValueError:
        return None


async def _edit_or_reply(update: Update, text: str, markup=None) -> None:
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


# ─── Publish flow state helpers ─────────────────────────────────────────────

def _pub_state(context) -> dict:
    return context.user_data.setdefault("pub", {})


def _pub_clear(context) -> None:
    context.user_data.pop("pub", None)


# ─── Screens ────────────────────────────────────────────────────────────────

async def show_home(update, context) -> None:
    role = _role(context, update.effective_user.id)
    await _edit_or_reply(update, WELCOME, main_menu_kb(role))


async def show_help(update, context) -> None:
    await _edit_or_reply(update, HELP, back_home_kb())


async def show_stats(update, context) -> None:
    _, db = _deps(context)
    s = db.stats()
    text = (
        "📊 <b>Dashboard</b>\n────────────────────────\n"
        f"📡 Channels: <b>{s['channels_enabled']}</b> / {s['channels_total']} on\n"
        f"👥 Admins: <b>{s['admins']}</b>\n"
        f"📋 Active: <b>{s['promotions_active']}</b>\n"
        f"📦 Total promos: <b>{s['promotions_total']}</b>\n"
        f"⏳ Pending deletes: <b>{s['messages_pending']}</b>\n"
        f"✅ Deleted msgs: <b>{s['messages_deleted']}</b>\n"
        f"❌ Failed deletes: <b>{s['messages_failed']}</b>"
    )
    await _edit_or_reply(update, text, back_home_kb())


async def show_admins(update, context) -> None:
    settings, db = _deps(context)
    ids = [str(d["user_id"]) for d in db.admins.find({}, {"_id": 0, "user_id": 1})]
    text = (
        "👥 <b>Administrators</b>\n────────────────────────\n"
        f"👑 Owner: <code>{settings.owner_id}</code>\n"
        f"🛡️ Admins: {', '.join(f'<code>{i}</code>' for i in ids) or 'None'}"
    )
    role = _role(context, update.effective_user.id)
    rows = [[InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")]]
    if role >= Role.OWNER:
        rows.insert(0, [InlineKeyboardButton("➕ Add/Remove help", callback_data="panel:manage_admins")])
    await _edit_or_reply(update, text, _kb(rows))


async def show_channels(update, context, page: int = 0) -> None:
    _, db = _deps(context)
    all_ch = list(db.channels.find({}, {"_id": 0}).sort("title", 1))
    total = len(all_ch)
    if total == 0:
        await _edit_or_reply(
            update,
            "📭 <b>No channels</b>\n────────────────────────\nOwner: /addchannel @name",
            back_home_kb(),
        )
        return
    start = page * PAGE_SIZE
    chunk = all_ch[start : start + PAGE_SIZE]
    lines = [f"📡 <b>Channels</b> ({total})\n────────────────────────"]
    buttons = []
    for c in chunk:
        status = "🟢" if c.get("enabled") else "🔴"
        title = (c.get("title") or "Unknown")[:26]
        err = f"\n   ⚠️ {str(c.get('last_error', ''))[:50]}" if c.get("last_error") else ""
        lines.append(f"{status} <b>{title}</b>\n   <code>{c['chat_id']}</code>{err}")
        toggle = "Disable" if c.get("enabled") else "Enable"
        buttons.append([
            InlineKeyboardButton(f"{toggle} · {title[:16]}", callback_data=f"ch:toggle:{c['chat_id']}:{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ch:list:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"ch:list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")])
    await _edit_or_reply(update, "\n\n".join(lines), _kb(buttons))


async def show_promotions(update, context, page: int = 0, history: bool = False) -> None:
    _, db = _deps(context)
    query = {"status": "closed"} if history else {"status": "active"}
    title = "📜 History" if history else "📋 Active Promotions"
    prefix = "pr:hist" if history else "pr:list"
    all_p = list(db.promotions.find(query, {"_id": 0}).sort("created_at", -1))
    total = len(all_p)
    if total == 0:
        await _edit_or_reply(
            update,
            f"{title}\n────────────────────────\nKuch nahi mila.",
            back_home_kb(),
        )
        return
    start = page * PAGE_SIZE
    chunk = all_p[start : start + PAGE_SIZE]
    lines = [f"{title} ({total})\n────────────────────────"]
    buttons = []
    for p in chunk:
        pid = p["promotion_id"]
        mode = format_duration(p.get("mode_minutes"))
        pub = p.get("published", 0)
        fail_n = len(p.get("failed") or [])
        if history:
            lines.append(f"<b>{pid}</b>\n   {pub} sent · {fail_n} fail · {mode}")
            buttons.append([InlineKeyboardButton(f"ℹ️ {pid}", callback_data=f"pr:detail:{pid}")])
        else:
            nxt = db.promotion_messages.find_one(
                {"promotion_id": pid, "status": "pending"},
                {"_id": 0, "delete_at": 1},
                sort=[("delete_at", 1)],
            )
            when = f"⏰ {format_ist(nxt['delete_at'])}" if nxt else "✅ done"
            lines.append(f"<b>{pid}</b>\n   {pub} live · {fail_n} fail · {mode}\n   {when}")
            row = [InlineKeyboardButton(f"🗑 {pid}", callback_data=f"pr:delask:{pid}")]
            if fail_n:
                row.append(InlineKeyboardButton("🔄 Retry", callback_data=f"pr:retry:{pid}"))
            buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        buttons.append(nav)
    if not history:
        buttons.append([InlineKeyboardButton("🗑 Delete ALL active", callback_data="pr:delallask")])
    buttons.append([InlineKeyboardButton("◀️ Main Menu", callback_data="panel:home")])
    await _edit_or_reply(update, "\n\n".join(lines), _kb(buttons))


async def show_promo_detail(update, context, promo_id: str) -> None:
    _, db = _deps(context)
    p = db.promotions.find_one({"promotion_id": promo_id}, {"_id": 0})
    if not p:
        await _edit_or_reply(update, "⚠️ Promotion nahi mili.", back_home_kb())
        return
    fails = p.get("failed") or []
    lines = [
        f"ℹ️ <b>{promo_id}</b>\n────────────────────────",
        f"Status: <b>{p.get('status')}</b>",
        f"Published: <b>{p.get('published', 0)}</b> / {p.get('targets', '?')}",
        f"Mode: {format_duration(p.get('mode_minutes'))}",
        f"Created: {p.get('created_at', '')}",
    ]
    if fails:
        lines.append("\n<b>Failed channels:</b>")
        for f in fails[:20]:
            lines.append(f"• <code>{f['chat_id']}</code>: {f.get('reason', '')[:60]}")
    # Per-channel message status
    msgs = list(db.promotion_messages.find({"promotion_id": promo_id}, {"_id": 0, "chat_id": 1, "status": 1}))
    if msgs:
        by_status: dict[str, int] = {}
        for m in msgs:
            by_status[m.get("status", "?")] = by_status.get(m.get("status", "?"), 0) + 1
        lines.append("\n<b>Messages:</b> " + ", ".join(f"{k}={v}" for k, v in by_status.items()))
    rows = []
    if fails and p.get("status") == "active":
        rows.append([InlineKeyboardButton("🔄 Retry failed", callback_data=f"pr:retry:{promo_id}")])
    if p.get("status") == "active":
        rows.append([InlineKeyboardButton("🗑 Delete now", callback_data=f"pr:delask:{promo_id}")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="pr:list:0")])
    await _edit_or_reply(update, "\n".join(lines), _kb(rows))


# ─── Publish multi-select channel UI ────────────────────────────────────────

async def show_pub_channels(update, context, page: int = 0) -> None:
    """Channel multi-select during publish flow. Supports 100+ channels with pages."""
    _, db = _deps(context)
    st = _pub_state(context)
    selected = set(st.get("channels") or [])
    all_ch = list(db.channels.find({}, {"_id": 0}).sort("title", 1))
    if not all_ch:
        await _edit_or_reply(update, "⚠️ Koi channel nahi. Pehle /addchannel karo.", back_home_kb())
        return
    total = len(all_ch)
    start = page * PUB_CH_PAGE
    chunk = all_ch[start : start + PUB_CH_PAGE]
    lines = [
        "📡 <b>Channels select karo</b>\n────────────────────────",
        f"Selected: <b>{len(selected)}</b> / {total}",
        f"Page {page + 1} · Tap to toggle 👇",
    ]
    buttons = []
    for c in chunk:
        cid = c["chat_id"]
        on = cid in selected
        mark = "✅" if on else "⬜"
        en = "" if c.get("enabled") else " (off)"
        title = (c.get("title") or str(cid))[:22]
        buttons.append([
            InlineKeyboardButton(f"{mark} {title}{en}", callback_data=f"pub:ch:{cid}:{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pub:chpage:{page - 1}"))
    if start + PUB_CH_PAGE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pub:chpage:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("✅ ALL enabled", callback_data="pub:ch:all"),
        InlineKeyboardButton("Clear", callback_data="pub:ch:clear"),
    ])
    buttons.append([
        InlineKeyboardButton("Next: Time ➡️", callback_data="pub:goto_dur"),
        InlineKeyboardButton("❌ Cancel", callback_data="panel:home"),
    ])
    await _edit_or_reply(update, "\n".join(lines), _kb(buttons))


async def show_pub_preview(update, context) -> None:
    st = _pub_state(context)
    channels = st.get("channels") or []
    minutes = st.get("minutes")  # may be None = manual
    n_msg = len(st.get("message_ids") or [])
    send_mode = st.get("send_mode") or "copy"
    mode_label = "Forward (emoji safe)" if send_mode == "forward" else "Copy (clean)"
    text = (
        "👁 <b>Preview — Confirm?</b>\n"
        "────────────────────────\n"
        f"📨 Messages: <b>{n_msg}</b>\n"
        f"📡 Channels: <b>{len(channels)}</b>\n"
        f"⏰ Delete after: <b>{format_duration(minutes)}</b>\n"
        f"📤 Mode: <b>{mode_label}</b>\n\n"
        "Publish karna hai?"
    )
    await _edit_or_reply(
        update,
        text,
        _kb([
            [
                InlineKeyboardButton("✅ Confirm & Publish", callback_data="pub:confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="panel:home"),
            ],
            [
                InlineKeyboardButton("◀️ Time", callback_data="pub:goto_dur"),
                InlineKeyboardButton("◀️ Mode", callback_data="pub:goto_mode"),
            ],
        ]),
    )


async def show_send_mode(update, context) -> None:
    """Choose copy vs forward before preview."""
    text = (
        "📤 <b>Send mode</b>\n"
        "────────────────────────\n"
        "<b>Copy</b> — clean post, no Forwarded tag\n"
        "(custom emoji kabhi normal ho sakte hain)\n\n"
        "<b>Forward</b> — custom emoji safe\n"
        "(post pe Forwarded from dikhega)\n\n"
        "Text, photo, video, file, links — dono modes support."
    )
    await _edit_or_reply(
        update,
        text,
        _kb([
            [
                InlineKeyboardButton("📋 Copy", callback_data="pub:mode:copy"),
                InlineKeyboardButton("➡️ Forward", callback_data="pub:mode:forward"),
            ],
            [InlineKeyboardButton("◀️ Time", callback_data="pub:goto_dur")],
        ]),
    )


# ─── Commands ───────────────────────────────────────────────────────────────

async def cmd_start(update, context) -> None:
    context.user_data.clear()
    user = update.effective_user
    if user is None:
        return
    role = _role(context, user.id)
    if not is_allowed(role, Role.ADMIN):
        _, db = _deps(context)
        await update.effective_message.reply_text(db.get_guest_welcome())
        return
    await show_home(update, context)


async def cmd_help(update, context) -> None:
    await show_help(update, context)


@requires(Role.ADMIN)
async def cmd_stats(update, context) -> None:
    await show_stats(update, context)


@requires(Role.OWNER)
async def cmd_addadmin(update, context) -> None:
    await _change_admin(update, context, add=True)


@requires(Role.OWNER)
async def cmd_removeadmin(update, context) -> None:
    await _change_admin(update, context, add=False)


async def _change_admin(update, context, add: bool) -> None:
    settings, db = _deps(context)
    target = _int_arg(context.args)
    if target is None:
        await update.effective_message.reply_text(
            "👉 <code>/addadmin 123456789</code>\n<code>/removeadmin 123456789</code>",
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
        await update.effective_message.reply_text(f"✅ Admin added: <code>{target}</code>", parse_mode=ParseMode.HTML)
    else:
        db.admins.delete_one({"user_id": target})
        await update.effective_message.reply_text(f"✅ Admin removed: <code>{target}</code>", parse_mode=ParseMode.HTML)
    _audit(db, actor, "add_admin" if add else "remove_admin", target)


@requires(Role.ADMIN)
async def cmd_admins(update, context) -> None:
    await show_admins(update, context)


async def _try_add_one_channel(bot, db, ref: str, actor_id: int) -> tuple[bool, str]:
    """Validate bot admin rights and upsert channel. Returns (ok, message)."""
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError as exc:
        return False, f"{ref}: {describe_error(exc)}"
    ok = me.status == "creator" or (
        me.status == "administrator" and getattr(me, "can_post_messages", False)
    )
    if not ok:
        return False, f"{chat.title or ref}: Bot ko Admin + Post + Delete do"
    current = db.channels.count_documents({})
    exists = db.channels.find_one({"chat_id": chat.id}) is not None
    if not exists and current >= MAX_CHANNELS:
        return False, f"Limit {MAX_CHANNELS} channels. Pehle kuchh remove karo."
    db.channels.update_one(
        {"chat_id": chat.id},
        {
            "$set": {"title": chat.title or str(chat.id), "username": getattr(chat, "username", None), "enabled": True},
            "$setOnInsert": {"added_by": actor_id, "added_at": utcnow()},
        },
        upsert=True,
    )
    return True, f"{chat.title or chat.id}"


@requires(Role.OWNER)
async def cmd_addchannel(update, context) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text(
            "👉 <b>Channel add</b>\n"
            "────────────────────────\n"
            "1) Bot ko channel mein <b>Admin</b> banao (Post + Delete)\n"
            "2) Bot auto-add ho sakta hai jab aap Admin banate ho\n"
            "3) Ya command se:\n"
            "<code>/addchannel @name</code>\n"
            "<code>/addchannel @a @b @c</code>  (ek saath kai)\n"
            "<code>/addchannel -100123...</code>\n\n"
            f"Max channels: <b>{MAX_CHANNELS}</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    actor = update.effective_user.id
    ok_list, fail_list = [], []
    for ref in context.args[:MAX_CHANNELS]:
        good, msg = await _try_add_one_channel(context.bot, db, ref, actor)
        (ok_list if good else fail_list).append(msg)
        if good:
            _audit(db, actor, "add_channel", msg)
    lines = [f"✅ <b>Added ({len(ok_list)})</b>"]
    for m in ok_list[:30]:
        lines.append(f"• {m}")
    if fail_list:
        lines.append(f"\n❌ <b>Failed ({len(fail_list)})</b>")
        for m in fail_list[:20]:
            lines.append(f"• {m}")
    total = db.channels.count_documents({})
    lines.append(f"\n📡 Total channels: <b>{total}</b> / {MAX_CHANNELS}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@requires(Role.OWNER)
async def cmd_removechannel(update, context) -> None:
    _, db = _deps(context)
    chat_id = _int_arg(context.args)
    if chat_id is None:
        await update.effective_message.reply_text(
            "👉 <code>/removechannel -1001234567890</code>", parse_mode=ParseMode.HTML
        )
        return
    db.channels.delete_one({"chat_id": chat_id})
    _audit(db, update.effective_user.id, "remove_channel", chat_id)
    await update.effective_message.reply_text("✅ Channel removed.")


@requires(Role.ADMIN)
async def cmd_channels(update, context) -> None:
    await show_channels(update, context, 0)


@requires(Role.ADMIN)
async def cmd_publish(update, context) -> None:
    """Start publish flow: reply required. Optional duration arg = quick path to all enabled."""
    msg = update.effective_message
    src = msg.reply_to_message
    if src is None:
        await msg.reply_text(PUBLISH_HELP, parse_mode=ParseMode.HTML, reply_markup=back_home_kb())
        return

    # Collect album message IDs if media_group
    message_ids = [src.message_id]
    if src.media_group_id and msg.chat:
        # Best-effort: only the replied message is guaranteed; album siblings
        # may not all be in update. User can reply to each or we use single.
        # Store media_group_id for info; copy still works per message.
        pass

    st = _pub_state(context)
    st.clear()
    st["source_chat_id"] = msg.chat_id
    st["message_ids"] = message_ids
    st["media_group_id"] = getattr(src, "media_group_id", None)

    # Quick path: /publish 24h → all enabled channels, skip multi-select
    if context.args:
        try:
            minutes = None if context.args[0].lower() == "manual" else parse_duration_minutes(context.args[0])
        except ValueError as exc:
            await msg.reply_text(f"⚠️ {exc}")
            return
        _, db = _deps(context)
        channels = [c["chat_id"] for c in db.channels.find({"enabled": True}, {"_id": 0, "chat_id": 1})]
        st["channels"] = channels
        st["minutes"] = minutes
        await show_pub_preview(update, context)
        return

    # Full flow: channel select first
    _, db = _deps(context)
    # Pre-select all enabled
    st["channels"] = [c["chat_id"] for c in db.channels.find({"enabled": True}, {"_id": 0, "chat_id": 1})]
    await show_pub_channels(update, context)


@requires(Role.ADMIN)
async def cmd_promotions(update, context) -> None:
    await show_promotions(update, context, 0, history=False)


@requires(Role.ADMIN)
async def cmd_history(update, context) -> None:
    await update.effective_message.reply_text(
        "ℹ️ Delete ke baad history save nahi hoti (DB clean).\nSirf Active promotions dekho: /promotions"
    )


@requires(Role.ADMIN)
async def cmd_delete(update, context) -> None:
    _, db = _deps(context)
    if not context.args:
        await update.effective_message.reply_text(
            "👉 <code>/delete P-ID</code> phir <code>/delete P-ID yes</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    promo_id = context.args[0]
    promo = db.promotions.find_one({"promotion_id": promo_id, "status": {"$ne": "closed"}})
    if promo is None:
        await update.effective_message.reply_text("⚠️ Open promotion nahi mili.")
        return
    pending = db.promotion_messages.count_documents({"promotion_id": promo_id, "status": "pending"})
    if len(context.args) < 2 or context.args[1].lower() != "yes":
        await update.effective_message.reply_text(
            f"⚠️ <b>{pending}</b> msgs delete honge.\nConfirm: <code>/delete {promo_id} yes</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb([[
                InlineKeyboardButton("✅ Yes", callback_data=f"pr:delconfirm:{promo_id}"),
                InlineKeyboardButton("❌ No", callback_data="pr:list:0"),
            ]]),
        )
        return
    await _run_delete_one(update, context, promo_id)


async def _run_delete_one(update, context, promo_id: str) -> None:
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
    for f in counts["failed"][:10]:
        lines.append(f"❌ {f['chat_id']}: {f['reason']}")
    await _edit_or_reply(update, "\n".join(lines), back_home_kb())


async def _run_publish(update, context) -> None:
    st = _pub_state(context)
    channels = st.get("channels") or []
    minutes = st.get("minutes")
    source_chat_id = st.get("source_chat_id")
    message_ids = st.get("message_ids") or []
    if not channels or not source_chat_id or not message_ids:
        await _edit_or_reply(update, "⚠️ Publish data missing. /publish se dobara shuru karo.", back_home_kb())
        _pub_clear(context)
        return

    status_msg = None
    target = update.effective_message or (
        update.callback_query.message if update.callback_query else None
    )
    if target:
        status_msg = await target.reply_text(
            f"🚀 Publishing to <b>{len(channels)}</b> channel(s)...",
            parse_mode=ParseMode.HTML,
        )

    send_mode = st.get("send_mode") or "copy"
    try:
        promo_id, ok, failures = await publish_promotion(
            context.bot,
            context.application.bot_data["db"],
            source_chat_id,
            message_ids,
            channels,
            minutes,
            update.effective_user.id,
            send_mode=send_mode,
        )
    except (PyMongoError, ValueError) as exc:
        if status_msg:
            await status_msg.edit_text(f"⚠️ Publish failed: {exc}")
        _pub_clear(context)
        return

    _audit(context.application.bot_data["db"], update.effective_user.id, "publish", promo_id)
    lines = [
        f"✅ <b>{promo_id}</b>",
        f"Published: <b>{ok}</b> / {len(channels)}",
        f"Delete: {format_duration(minutes)}",
        f"Send: {'Forward' if send_mode == 'forward' else 'Copy'}",
    ]
    for f in failures[:15]:
        lines.append(f"❌ <code>{f['chat_id']}</code>: {f['reason']}")
    if failures:
        lines.append("\n🔄 Failed channels: Active list se Retry kar sakte ho.")
    text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            await status_msg.reply_text(text, parse_mode=ParseMode.HTML)
    _pub_clear(context)


@requires(Role.OWNER)
async def cmd_setwelcome(update, context) -> None:
    """Owner sets the message shown to non-admin users on /start."""
    _, db = _deps(context)
    raw = (update.effective_message.text or "")
    # Everything after the command
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        current = db.get_guest_welcome()
        await update.effective_message.reply_text(
            "👉 Guest welcome message set karne ke liye:\n"
            "<code>/setwelcome Aapka message yahan</code>\n\n"
            f"<b>Abhi ka message:</b>\n{current}",
            parse_mode=ParseMode.HTML,
        )
        return
    msg = parts[1].strip()
    if len(msg) > 3500:
        await update.effective_message.reply_text("⚠️ Message bahut lamba hai (max ~3500 chars).")
        return
    db.set_setting("guest_welcome", msg)
    _audit(db, update.effective_user.id, "set_welcome", msg[:80])
    await update.effective_message.reply_text(
        "✅ Guest welcome message save ho gaya.\nNon-admin /start pe yahi dikhega."
    )


async def cmd_cancel(update, context) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text("✅ Cancelled.", reply_markup=back_home_kb())


# ─── Callback router ────────────────────────────────────────────────────────

async def on_callback(update: Update, context) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user = update.effective_user
    if not user:
        return
    role = _role(context, user.id)

    # Panel
    if data == "panel:home":
        _pub_clear(context)
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
            "➕ <b>Add Channel</b>\n────────────────────────\n"
            "• Bot ko channel mein <b>Admin</b> banao\n"
            "  (Post + Delete) → <b>auto-add</b> ho jayega\n\n"
            "• Ya command:\n"
            "<code>/addchannel @name</code>\n"
            "<code>/addchannel @a @b @c</code>\n\n"
            f"Max: <b>{MAX_CHANNELS}</b> channels",
            back_home_kb(),
        )
        return
    if data == "panel:manage_admins":
        if not is_allowed(role, Role.OWNER):
            await query.answer("🔒 Owner only", show_alert=True)
            return
        await _edit_or_reply(
            update,
            "🛡️ <b>Admins</b>\n────────────────────────\n"
            "<code>/addadmin ID</code>\n<code>/removeadmin ID</code>",
            back_home_kb(),
        )
        return

    # Channels list / toggle
    if data.startswith("ch:list:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        page = int(data.split(":")[2])
        await show_channels(update, context, page)
        return
    if data.startswith("ch:toggle:"):
        if not is_allowed(role, Role.OWNER):
            await query.answer("🔒 Owner only", show_alert=True)
            return
        parts = data.split(":")
        chat_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        _, db = _deps(context)
        ch = db.channels.find_one({"chat_id": chat_id})
        if ch:
            new_val = not ch.get("enabled", True)
            db.channels.update_one({"chat_id": chat_id}, {"$set": {"enabled": new_val}})
            _audit(db, user.id, "toggle_channel", f"{chat_id}:{new_val}")
        await show_channels(update, context, page)
        return

    # Promotions list / history / detail
    if data.startswith("pr:list:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_promotions(update, context, int(data.split(":")[2]), history=False)
        return
    if data.startswith("pr:hist:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_promotions(update, context, int(data.split(":")[2]), history=True)
        return
    if data.startswith("pr:detail:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await show_promo_detail(update, context, data.split(":", 2)[2])
        return
    if data.startswith("pr:delask:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        pid = data.split(":", 2)[2]
        _, db = _deps(context)
        pending = db.promotion_messages.count_documents({"promotion_id": pid, "status": "pending"})
        await _edit_or_reply(
            update,
            f"⚠️ Delete <b>{pid}</b>?\n<b>{pending}</b> messages hatenge.",
            _kb([[
                InlineKeyboardButton("✅ Yes", callback_data=f"pr:delconfirm:{pid}"),
                InlineKeyboardButton("❌ No", callback_data="pr:list:0"),
            ]]),
        )
        return
    if data.startswith("pr:delconfirm:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await _run_delete_one(update, context, data.split(":", 2)[2])
        return
    if data == "pr:delallask":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        await _edit_or_reply(
            update,
            "⚠️ <b>Saari active promotions</b> delete?\nYeh undo nahi hoga.",
            _kb([[
                InlineKeyboardButton("✅ Delete ALL", callback_data="pr:delallconfirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="pr:list:0"),
            ]]),
        )
        return
    if data == "pr:delallconfirm":
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        _, db = _deps(context)
        ids = [p["promotion_id"] for p in db.promotions.find({"status": "active"}, {"promotion_id": 1})]
        if not ids:
            await query.answer("Koi active nahi", show_alert=True)
            return
        await query.answer("Deleting all...")
        total = await delete_many_promotions(context.bot, db, ids)
        _audit(db, user.id, "delete_all", f"{total['promos']} promos")
        await _edit_or_reply(
            update,
            f"🗑 <b>All active deleted</b>\n"
            f"Promos: {total['promos']}\n"
            f"✅ {total['deleted']} · gone {total['already_gone']} · retry {total['pending']}",
            back_home_kb(),
        )
        return
    if data.startswith("pr:retry:"):
        if not is_allowed(role, Role.ADMIN):
            await query.answer("🔒 Admin only", show_alert=True)
            return
        pid = data.split(":", 2)[2]
        await query.answer("Retrying...")
        recovered, still = await retry_failed_channels(context.bot, context.application.bot_data["db"], pid)
        _audit(context.application.bot_data["db"], user.id, "retry", pid)
        lines = [f"🔄 <b>{pid}</b>", f"Recovered: <b>{recovered}</b>", f"Still failed: <b>{len(still)}</b>"]
        for f in still[:10]:
            lines.append(f"❌ {f['chat_id']}: {f.get('reason', '')[:50]}")
        await _edit_or_reply(update, "\n".join(lines), back_home_kb())
        return

    # ── Publish flow callbacks
    if not is_allowed(role, Role.ADMIN) and data.startswith("pub:"):
        await query.answer("🔒 Admin only", show_alert=True)
        return

    if data == "pub:channels":
        await show_pub_channels(update, context, 0)
        return
    if data.startswith("pub:chpage:"):
        await show_pub_channels(update, context, int(data.split(":")[2]))
        return
    if data.startswith("pub:ch:"):
        st = _pub_state(context)
        selected = set(st.get("channels") or [])
        parts = data.split(":")
        key = parts[2]
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        if key == "all":
            _, db = _deps(context)
            selected = {c["chat_id"] for c in db.channels.find({"enabled": True}, {"chat_id": 1})}
        elif key == "clear":
            selected = set()
        else:
            cid = int(key)
            if cid in selected:
                selected.discard(cid)
            else:
                selected.add(cid)
        st["channels"] = list(selected)
        await show_pub_channels(update, context, page)
        return
    if data == "pub:goto_dur":
        st = _pub_state(context)
        if not st.get("channels"):
            await query.answer("Pehle kam se kam 1 channel select karo.", show_alert=True)
            return
        await _edit_or_reply(
            update,
            "⏰ <b>Delete time</b>\n────────────────────────\nKitni der baad delete ho?",
            duration_kb(),
        )
        return
    if data.startswith("pub:dur:"):
        st = _pub_state(context)
        key = data.split(":", 2)[2]
        if key == "manual":
            st["minutes"] = None
        else:
            st["minutes"] = int(key)
        await show_send_mode(update, context)
        return
    if data == "pub:goto_mode":
        await show_send_mode(update, context)
        return
    if data.startswith("pub:mode:"):
        st = _pub_state(context)
        st["send_mode"] = data.split(":")[2]
        await show_pub_preview(update, context)
        return
    if data == "pub:confirm":
        await _run_publish(update, context)
        return



async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When bot is added/promoted as channel admin, auto-register the channel."""
    mcm = update.my_chat_member
    if mcm is None:
        return
    chat = mcm.chat
    if chat.type != "channel":
        return
    new = mcm.new_chat_member
    if new.user.id != context.bot.id:
        return
    _, db = _deps(context)
    status = new.status
    if status in ("administrator", "creator"):
        can_post = getattr(new, "can_post_messages", True)
        if status == "creator" or can_post:
            current = db.channels.count_documents({})
            exists = db.channels.find_one({"chat_id": chat.id}) is not None
            if not exists and current >= MAX_CHANNELS:
                log.warning("Auto-add skipped %s: at MAX_CHANNELS", chat.id)
                return
            db.channels.update_one(
                {"chat_id": chat.id},
                {
                    "$set": {
                        "title": chat.title or str(chat.id),
                        "username": getattr(chat, "username", None),
                        "enabled": True,
                    },
                    "$setOnInsert": {"added_by": 0, "added_at": utcnow()},
                },
                upsert=True,
            )
            log.info("Auto-added channel %s (%s)", chat.id, chat.title)
    elif status in ("left", "kicked"):
        db.channels.delete_one({"chat_id": chat.id})
        log.info("Auto-removed channel %s", chat.id)


async def on_error(update, context) -> None:
    settings = context.application.bot_data["settings"]
    log.error(
        "Unhandled %s: %s",
        type(context.error).__name__,
        str(context.error).replace(settings.bot_token, "***")[:300],
    )
    if isinstance(update, Update) and update.effective_message:
        text = (
            "⚠️ Database temporarily unavailable."
            if isinstance(context.error, PyMongoError)
            else "⚠️ Something went wrong."
        )
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            pass


async def _post_init(app: Application) -> None:
    settings = app.bot_data["settings"]
    db = app.bot_data["db"]
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
    for name, handler in {
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
        "history": cmd_history,
        "delete": cmd_delete,
        "cancel": cmd_cancel,
        "setwelcome": cmd_setwelcome,
    }.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    return app
