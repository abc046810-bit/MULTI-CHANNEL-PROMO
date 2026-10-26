# Telegram Multi-Channel Promotion Bot

Publishes a promotion to many channels at once and deletes every copy later.
Every copy has a hard deletion deadline of **45 hours** from its own publication time, even in manual mode.

## Setup (step by step)

1. **Bot token:** In Telegram, open @BotFather, send `/newbot`, and copy the token. This is `BOT_TOKEN`.
2. **Owner ID:** Message @userinfobot and copy your numeric ID. This is `OWNER_ID`.
3. **MongoDB Atlas:** Create a free cluster, create a database user, and under Network Access allow `0.0.0.0/0` (Render's IPs change). Copy the connection string into `MONGODB_URI`.
4. **Channels:** Add the bot to each channel as an **administrator** with "Post messages" and "Delete messages" enabled.
5. **Deploy on Render (free web service):** Push this folder to GitHub, then in Render choose New → Blueprint (or Web Service with Build `pip install -r requirements.txt` and Start `python app.py`). Set `BOT_TOKEN`, `MONGODB_URI`, and `OWNER_ID`. `WEBHOOK_SECRET` is generated automatically.
6. The bot registers its webhook automatically at `https://<your-service>.onrender.com/telegram`.

## Commands (send them to the bot in a private chat)

- `/addchannel @name` or `/addchannel -100123...`: register a channel (the bot checks its admin rights).
- `/removechannel -100123...`, `/channels`
- `/addadmin 123456789`, `/removeadmin 123456789`, `/admins` (owner only)
- `/publish 24h` (or `90m`, `45h`, or `manual`): reply to the content you want to publish. Sends to all enabled channels.
- `/promotions`: active promotions and their next deletion time (UTC).
- `/delete P-20261026-AB12`, then `/delete P-20261026-AB12 yes` to confirm.
- `/help`, `/cancel`

Duration rules: minimum 1 minute, maximum 45 hours. Anything else is rejected by the backend.

## How deletion works

Each published copy gets its own record in MongoDB with `delete_at = published_at + min(chosen duration, 45h)`. Manual mode uses 45h as its safety deadline. A background scheduler checks MongoDB every 60 seconds, claims due records, and calls Telegram `deleteMessage`. Only a Telegram success (or "message not found") is recorded as done. Permanent failures notify the owner.

## Render free-tier limitations (read this)

- The free service sleeps when idle. Deletions that fall due while it sleeps run when the next request wakes it. They are late, not lost.
- Deadlines are set at 45h so there is roughly a 3-hour margin before Telegram's 48-hour limit. A sleeping service can miss that margin if nothing wakes it for longer.
- The scheduler state lives in MongoDB, so a restart or redeploy resumes overdue deletions automatically.
- Telegram cannot delete messages older than 48 hours through the Bot API. The bot cannot keep a promotion longer than 45 hours.
- MongoDB and Telegram are not one transaction. If the bot posts a message and crashes before saving its record, that copy is untracked. Check channels after a crash.

## Known limitations of this build

- Commands instead of an inline-button panel. Buttons and a multi-select channel picker are not included.
- Media albums are published as individual copies, and each one is tracked separately.
- Not run against real Telegram, MongoDB, or Render. See the test section.

## Testing

```
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

The tests cover duration validation (45h accepted, above 45h / zero / negative / malformed rejected), deletion deadlines, role checks, and owner protection. They do not contact Telegram or MongoDB.

## Security

Never commit `.env` or real tokens. Logs redact the bot token. Only the owner can manage admins and channels. Admins can publish and delete promotions.
