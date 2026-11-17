from datetime import date, datetime

MAX_SCHEDULE_DAYS = 5


def validate_schedule_date(date_str: str, today: date) -> str | None:
    try:
        d = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except ValueError:
        return "invalid_date"
    if not 0 <= (d - today).days <= MAX_SCHEDULE_DAYS:
        return "date_out_of_range"
    return None
