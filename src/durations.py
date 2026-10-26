"""Duration parsing and deletion deadlines. The 45-hour cap is enforced here, in backend code."""
from __future__ import annotations

from datetime import datetime, timedelta

MIN_MINUTES = 1
MAX_MINUTES = 45 * 60  # Hard cap, inside Telegram's 48-hour deletion window.
SAFETY_LIMIT = timedelta(minutes=MAX_MINUTES)

# Preset options for the premium UI (label, minutes or None for manual)
DURATION_PRESETS: list[tuple[str, int | None]] = [
    ("30m", 30),
    ("1h", 60),
    ("2h", 120),
    ("6h", 360),
    ("12h", 720),
    ("24h", 1440),
    ("36h", 2160),
    ("40h", 2400),
    ("44h", 2640),
    ("45h", 2700),
    ("Manual", None),
]


def validate_minutes(minutes: int) -> int:
    if minutes < MIN_MINUTES:
        raise ValueError("Duration must be at least 1 minute.")
    if minutes > MAX_MINUTES:
        raise ValueError("Maximum automatic lifetime is 45 hours.")
    return minutes


def parse_duration_minutes(raw: str) -> int:
    text = raw.strip().lower()
    if not text:
        raise ValueError("Duration is empty. Examples: 24h, 90m, 45h.")
    factor = 1
    if text.endswith("h"):
        text, factor = text[:-1], 60
    elif text.endswith("m"):
        text = text[:-1]
    if not text.isdigit():
        raise ValueError("Use a whole number with an optional h or m suffix, e.g. 24h or 90m.")
    return validate_minutes(int(text) * factor)


def deletion_deadline(published_at: datetime, mode_minutes: int | None) -> datetime:
    """mode_minutes=None means manual mode, which still gets the 45-hour safety deadline."""
    if mode_minutes is None:
        return published_at + SAFETY_LIMIT
    return published_at + min(timedelta(minutes=validate_minutes(mode_minutes)), SAFETY_LIMIT)


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "Manual (45h safety)"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"
