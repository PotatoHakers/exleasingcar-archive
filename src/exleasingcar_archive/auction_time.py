"""Display Exleasingcar's naive auction clock in the owner's local time.

The site clock is one hour behind the owner's local clock. Source values remain
unchanged in SQLite; this module only derives user-facing text.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

SITE_TO_USER_TIME_OFFSET = timedelta(hours=1)
_CLOCK_PATTERN = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)(?!\d)")
_DAY_WORDS = (
    ("сегодня", "завтра", "послезавтра"),
    ("today", "tomorrow", "the day after tomorrow"),
)


def _replace_day_marker(value: str, old: str, new: str) -> str:
    pattern = re.compile(re.escape(old), re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if original.isupper():
            return new.upper()
        if original[:1].isupper():
            return new.capitalize()
        return new

    return pattern.sub(replace, value, count=1)


def format_auction_time_for_user(value: str | None) -> str | None:
    """Shift a site-local auction date/time or clock label by the agreed +1 hour.

    ISO-like absolute date/time strings keep their original shape and precision.
    Relative labels (for example, "Конец Сегодня 12:58") keep their wording,
    with the day marker advanced when the +1 hour crosses midnight.
    Unrecognized text is returned unchanged rather than guessed.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("auction time must be str or None")

    raw = value.strip()
    if not raw:
        return raw

    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None

    if parsed is not None:
        # Explicitly zoned timestamps are instants, not the site's naive wall
        # clock; leave them untouched until a source timezone is configured.
        if parsed.tzinfo is not None:
            return raw
        shifted = parsed + SITE_TO_USER_TIME_OFFSET
        has_seconds = re.search(r":\d{2}:\d{2}(?:\.|$)", raw) is not None
        timespec = "microseconds" if shifted.microsecond else (
            "seconds" if has_seconds else "minutes"
        )
        return shifted.isoformat(
            sep=" " if " " in raw else "T",
            timespec=timespec,
        )

    match = _CLOCK_PATTERN.search(raw)
    if match is None:
        return raw

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23:
        return raw

    total_minutes = hour * 60 + minute + int(
        SITE_TO_USER_TIME_OFFSET.total_seconds() // 60
    )
    day_shift, shifted_minutes = divmod(total_minutes, 24 * 60)
    shifted_hour, shifted_minute = divmod(shifted_minutes, 60)
    clock_text = f"{shifted_hour:02d}:{shifted_minute:02d}"
    result = raw[: match.start()] + clock_text + raw[match.end() :]

    if day_shift:
        for today, tomorrow, after_tomorrow in _DAY_WORDS:
            if re.search(re.escape(tomorrow), result, re.IGNORECASE):
                result = _replace_day_marker(result, tomorrow, after_tomorrow)
                break
            if re.search(re.escape(today), result, re.IGNORECASE):
                result = _replace_day_marker(result, today, tomorrow)
                break

    return result
