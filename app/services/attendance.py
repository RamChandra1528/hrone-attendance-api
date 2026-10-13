"""Attendance business rules and derived-value calculations."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.core.config import IST
from app.utils.dates import as_utc, shift_time_on

def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_date: str
) -> int:
    """Apply R2's strict ten-minute grace period using the shift-start instant."""
    shift_start_at = shift_time_on(attendance_date, shift_start)
    elapsed_seconds = int(
        (as_utc(punch_in).astimezone(IST) - shift_start_at).total_seconds()
    )
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Apply R4's two-decimal, half-up rounding without float rounding errors."""
    elapsed_seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """Apply R3, moving an overnight shift end to the next IST calendar day."""
    shift_end_at = shift_time_on(attendance_date, shift_end)
    if shift_end <= shift_start:
        shift_end_at += timedelta(days=1)
    elapsed_seconds = int(
        (as_utc(punch_out).astimezone(IST) - shift_end_at).total_seconds()
    )
    elapsed_minutes = max(0, elapsed_seconds // 60)
    return elapsed_minutes if elapsed_minutes >= 30 else 0

