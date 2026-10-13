"""Date, timezone, and request-date validation helpers."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.config import DATE_PATTERN, IST, MONTH_PATTERN

def validate_calendar_date(value: str) -> str:
    """Accept only a real calendar date written exactly as YYYY-MM-DD."""
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("must be a YYYY-MM-DD date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("must be a real calendar date") from exc
    return value

def epoch_millis(value: datetime) -> int:
    """Serialize a MongoDB UTC datetime at the API boundary."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def instant_from_epoch_millis(value: Optional[int]) -> datetime:
    """Convert an API instant to UTC and truncate it to the stored second."""
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    seconds, _milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Treat legacy naive BSON datetimes as UTC before performing calculations."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Return the IST shift-start date required by R1, including overnight shifts."""
    local = as_utc(punch_in).astimezone(IST)
    end_hour, end_minute = map(int, shift_end.split(":"))
    is_before_overnight_end = (local.hour, local.minute, local.second) < (
        end_hour,
        end_minute,
        0,
    )
    if shift_end <= shift_start and is_before_overnight_end:
        return (local - timedelta(days=1)).date().isoformat()
    return local.date().isoformat()


def shift_time_on(attendance_date: str, shift_time: str) -> datetime:
    """Build an IST shift instant from the stored date and an HH:MM value."""
    hour, minute = map(int, shift_time.split(":"))
    return datetime.fromisoformat(attendance_date).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

def require_date(value: str, parameter: str) -> str:
    """Validate a query/path calendar date and report it as a FastAPI 422."""
    try:
        return validate_calendar_date(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"{parameter} must be a YYYY-MM-DD date"
        ) from exc


def require_month(value: str) -> str:
    """Validate the contract's YYYY-MM month form."""
    if not MONTH_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    return value


def month_bounds(month: str) -> tuple[str, str, str]:
    """Return the inclusive start/end and exclusive next-month date strings."""
    require_month(month)
    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1)
    if month_number == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month_number + 1, 1)
    return (
        start.date().isoformat(),
        (next_start - timedelta(days=1)).date().isoformat(),
        next_start.date().isoformat(),
    )
