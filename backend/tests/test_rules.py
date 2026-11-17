from datetime import date, timedelta

from app import rules

T = date(2026, 11, 18)


def test_today_and_plus5_ok():
    assert rules.validate_schedule_date(T.isoformat(), T) is None
    assert rules.validate_schedule_date((T + timedelta(days=5)).isoformat(), T) is None


def test_out_of_range():
    assert rules.validate_schedule_date((T + timedelta(days=6)).isoformat(), T) == "date_out_of_range"
    assert rules.validate_schedule_date((T - timedelta(days=1)).isoformat(), T) == "date_out_of_range"


def test_invalid():
    assert rules.validate_schedule_date("tomorrow", T) == "invalid_date"
    assert rules.MAX_SCHEDULE_DAYS == 5
