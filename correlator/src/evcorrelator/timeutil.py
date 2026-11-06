"""Timestamp parsing for the three sources."""

from __future__ import annotations

import datetime as dt
from typing import Any


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_ts(value: Any) -> dt.datetime | None:
    """ISO 8601 (`Z`, `+0000`, `+00:00`, naive = UTC) or epoch seconds; None if unusable.

    Alertmanager sends `0001-01-01T00:00:00Z` for an unset time: treated as unusable.
    """
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
        if isinstance(value, str) and value.strip():
            parsed = dt.datetime.fromisoformat(value.strip())
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
            parsed = parsed.astimezone(dt.UTC)
            return parsed if parsed.year > 2000 else None
    except (ValueError, OverflowError, OSError):
        return None
    return None
