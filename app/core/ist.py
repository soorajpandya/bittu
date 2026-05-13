"""
IST (Asia/Kolkata) helpers.

The backend stores all timestamps in UTC, but the product's calendar
("today", "this month", "from 1 May to 13 May") is always interpreted in
IST. These helpers centralise the conversion so date filters from the
frontend behave the same everywhere.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    """Current wall-clock time in IST (timezone-aware)."""
    return datetime.now(IST)


def ist_today() -> date:
    """Today's calendar date in IST (NOT the server's UTC today)."""
    return ist_now().date()


def parse_date(value: Union[str, date, datetime, None]) -> Optional[date]:
    """Coerce inbound API value to a date. Accepts date, datetime, or 'YYYY-MM-DD'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # Accept full ISO timestamps too — take the date portion in IST.
    if "T" in s or " " in s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(IST).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError as e:
        raise ValueError(f"invalid date: {value!r}") from e


def parse_datetime(value: Union[str, datetime, None]) -> Optional[datetime]:
    """Coerce inbound API value to a timezone-aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"invalid datetime: {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ist_day_start_utc(d: date) -> datetime:
    """Return the UTC instant corresponding to 00:00 IST on the given date."""
    return datetime.combine(d, time.min, tzinfo=IST).astimezone(timezone.utc)


def ist_day_end_utc(d: date) -> datetime:
    """Return the UTC instant corresponding to 00:00 IST on the day after `d`
    (exclusive upper bound for an IST calendar day)."""
    return datetime.combine(d + timedelta(days=1), time.min, tzinfo=IST).astimezone(timezone.utc)


def ist_range_utc(
    from_value: Union[str, date, datetime, None],
    to_value: Union[str, date, datetime, None],
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Convert a (from_date, to_date) pair to a UTC half-open window
    [from_utc, to_utc) where each bound is the IST midnight of that calendar day.

    `to_value` is treated as INCLUSIVE — the returned upper bound is midnight IST
    of the day AFTER it, so SQL should use `created_at < to_utc`.
    """
    fd = parse_date(from_value)
    td = parse_date(to_value)
    return (
        ist_day_start_utc(fd) if fd else None,
        ist_day_end_utc(td) if td else None,
    )
