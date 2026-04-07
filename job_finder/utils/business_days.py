"""Business day calculation for freshness filters."""

from datetime import date, timedelta


def business_days_ago(n: int, reference: date | None = None) -> date:
    """Return the date N business days before reference (default: today).

    Skips Saturdays (weekday 5) and Sundays (weekday 6).
    n=0 returns the reference date unchanged.
    """
    ref = reference or date.today()
    days_back = 0
    remaining = n
    while remaining > 0:
        days_back += 1
        if (ref - timedelta(days=days_back)).weekday() < 5:
            remaining -= 1
    return ref - timedelta(days=days_back)
