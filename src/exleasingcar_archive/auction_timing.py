"""Local countdown projection helpers.

Projected values are estimates for display and scheduling only. They must never
be used as source-confirmed auction completion events.
"""

from __future__ import annotations

from datetime import datetime, timezone


def project_remaining_seconds(
    remaining_seconds: int | None,
    observed_at: datetime | None,
    *,
    as_of: datetime | None = None,
    max_age_seconds: int = 300,
) -> int | None:
    """Project a recently observed countdown without contacting the website.

    Returns None when the site did not provide a timer, the observation is
    stale, or the timestamps cannot be compared safely.
    """
    if remaining_seconds is None or observed_at is None:
        return None
    if isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, int):
        raise TypeError("remaining_seconds must be an int or None")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        raise TypeError("max_age_seconds must be an int")
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds cannot be negative")
    if not isinstance(observed_at, datetime):
        raise TypeError("observed_at must be a datetime")
    current = as_of or datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise TypeError("as_of must be a datetime or None")
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("observed_at and as_of must be timezone-aware")

    elapsed = (current - observed_at).total_seconds()
    if elapsed < 0 or elapsed > max_age_seconds:
        return None

    return max(0, remaining_seconds - int(elapsed))
