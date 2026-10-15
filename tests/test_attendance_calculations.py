
from datetime import date, datetime, timezone
from decimal import Decimal

from app.main import (
    IST,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    get_attendance_date_and_shifts,
    round_half_up,
)


def test_late_grace_period_exactly_10_minutes():
    shift_start = datetime(2026, 10, 14, 9, 30, tzinfo=IST)
    punch_in = datetime(2026, 10, 14, 9, 40, tzinfo=IST)

    assert compute_late_minutes(punch_in, shift_start) == 0


def test_late_after_grace_period():
    shift_start = datetime(2026, 10, 14, 9, 30, tzinfo=IST)
    punch_in = datetime(2026, 10, 14, 9, 40, 1, tzinfo=IST)

    assert compute_late_minutes(punch_in, shift_start) == 10


def test_late_minutes_floor_whole_minutes():
    shift_start = datetime(2026, 10, 14, 9, 30, tzinfo=IST)
    punch_in = datetime(2026, 10, 14, 10, 15, 59, tzinfo=IST)

    assert compute_late_minutes(punch_in, shift_start) == 45


def test_work_hours_are_rounded_to_two_decimals():
    punch_in = datetime(2026, 10, 14, 9, 30, tzinfo=IST)
    punch_out = datetime(2026, 10, 14, 14, 0, tzinfo=IST)

    assert compute_work_hours(punch_in, punch_out) == 4.5


def test_work_hours_round_half_up():
    # 8 hours and 31 seconds is 8.0086... hours, which rounds to 8.01.
    punch_in = datetime(2026, 10, 14, 9, 30, tzinfo=IST)
    punch_out = datetime(2026, 10, 14, 17, 30, 31, tzinfo=IST)

    assert compute_work_hours(punch_in, punch_out) == 8.01


def test_overtime_under_30_minutes_is_zero():
    shift_end = datetime(2026, 10, 14, 18, 30, tzinfo=IST)
    punch_out = datetime(2026, 10, 14, 18, 59, 59, tzinfo=IST)

    assert compute_overtime(punch_out, shift_end) == 0


def test_overtime_exactly_30_minutes_counts():
    shift_end = datetime(2026, 10, 14, 18, 30, tzinfo=IST)
    punch_out = datetime(2026, 10, 14, 19, 0, tzinfo=IST)

    assert compute_overtime(punch_out, shift_end) == 30


def test_half_day_threshold():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(None) is False


def test_overnight_punch_in_belongs_to_previous_date():
    # 01:15 IST on Oct 14 belongs to the shift that began Oct 13 at 22:00.
    punch_in = datetime(2026, 10, 13, 19, 45, tzinfo=timezone.utc)

    attendance_date, shift_start, shift_end = (
        get_attendance_date_and_shifts(punch_in, "22:00", "06:00")
    )

    assert attendance_date == "2026-10-13"
    assert shift_start == datetime(2026, 10, 13, 22, 0, tzinfo=IST)
    assert shift_end == datetime(2026, 10, 14, 6, 0, tzinfo=IST)


def test_round_half_up():
    assert round_half_up(2.345, 2) == 2.35
