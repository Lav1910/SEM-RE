"""Date formats that work on every OS (Windows strftime has no %-d / %-I)."""
import datetime


def _dt(x):
    return datetime.datetime.fromtimestamp(x) if isinstance(x, (int, float)) else x


def long_date(x=None):
    """November 9, 2026"""
    d = _dt(x) if x is not None else datetime.date.today()
    return f"{d:%B} {d.day}, {d.year}"


def short_date(x):
    """Nov 9, 2026"""
    d = _dt(x)
    return f"{d:%b} {d.day}, {d.year}"


def month_day(x):
    """Nov 9"""
    d = _dt(x)
    return f"{d:%b} {d.day}"


def date_time(x):
    """Nov 9, 2026 · 8:42 PM"""
    d = _dt(x)
    return f"{short_date(d)} · {d.hour % 12 or 12}:{d:%M %p}"
