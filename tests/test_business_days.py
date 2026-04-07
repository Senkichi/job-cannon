"""Tests for business_days_ago utility."""

from datetime import date

from job_finder.utils.business_days import business_days_ago


def test_zero_returns_reference():
    assert business_days_ago(0, reference=date(2026, 4, 6)) == date(2026, 4, 6)


def test_one_biz_day_from_monday_is_friday():
    assert business_days_ago(1, reference=date(2026, 4, 6)) == date(2026, 4, 3)


def test_three_biz_days_from_monday():
    assert business_days_ago(3, reference=date(2026, 4, 6)) == date(2026, 4, 1)


def test_five_biz_days_is_full_week():
    assert business_days_ago(5, reference=date(2026, 4, 6)) == date(2026, 3, 30)


def test_from_friday():
    assert business_days_ago(1, reference=date(2026, 4, 3)) == date(2026, 4, 2)


def test_from_weekend_saturday():
    assert business_days_ago(1, reference=date(2026, 4, 4)) == date(2026, 4, 3)


def test_from_weekend_sunday():
    assert business_days_ago(1, reference=date(2026, 4, 5)) == date(2026, 4, 3)


def test_defaults_to_today(monkeypatch):
    fake_today = date(2026, 4, 6)
    monkeypatch.setattr("job_finder.utils.business_days.date", type(
        "MockDate", (date,), {"today": classmethod(lambda cls: fake_today)}
    ))
    assert business_days_ago(1) == date(2026, 4, 3)
