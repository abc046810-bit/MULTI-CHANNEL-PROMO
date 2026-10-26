"""Duration parsing and deletion deadlines. The 45-hour cap is enforced here, in backend code."""
from datetime import datetime, timedelta

MIN_MINUTES = 1
MAX_MINUTES = 45 * 60  # Hard cap, inside Telegram's 48-hour deletion window.
SAFETY_LIMIT = timedelta(minutes=MAX_MINUTES)


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
