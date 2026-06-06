"""Tests for Phase 49.02 — salary_currency + salary_period (m081) + emission."""

from __future__ import annotations

import sqlite3

import pytest

from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.ats_platforms._platforms_greenhouse import _posting_to_job
from job_finder.web.migrations._runner import _apply_migration
from job_finder.web.migrations.m081_salary_currency_period import MIGRATION as M081
from job_finder.web.migrations.types import MigrationContext

# ---------------------------------------------------------------------------
# m081 migration — columns, CHECK, suspect backfill
# ---------------------------------------------------------------------------


def _pre_m081_db(path: str) -> None:
    """jobs table at the pre-m081 schema (no salary_currency/period)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                salary_min INTEGER,
                unresolved_reasons TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO jobs (dedup_key, salary_min, unresolved_reasons) VALUES (?, ?, ?)",
            [
                ("normal", 180000, "[]"),
                ("low", 50, "[]"),  # < 1000 → suspect
                ("high", 5_000_000, None),  # > 1M → suspect
                ("nosal", None, "[]"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _apply(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        ctx = MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=80)
        _apply_migration(ctx, M081)
    finally:
        conn.close()


def test_m081_adds_columns_with_defaults(tmp_db_path):
    _pre_m081_db(tmp_db_path)
    _apply(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "salary_currency" in cols
        assert "salary_period" in cols
        cur, per = conn.execute(
            "SELECT salary_currency, salary_period FROM jobs WHERE dedup_key='normal'"
        ).fetchone()
        assert cur == "USD"
        assert per == "unknown"
    finally:
        conn.close()


def test_m081_flags_unit_suspect_rows(tmp_db_path):
    _pre_m081_db(tmp_db_path)
    _apply(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        rows = dict(conn.execute("SELECT dedup_key, unresolved_reasons FROM jobs").fetchall())
    finally:
        conn.close()
    assert "salary_unit_suspect" in rows["low"]
    assert "salary_unit_suspect" in rows["high"]
    assert "salary_unit_suspect" not in (rows["normal"] or "")
    assert "salary_unit_suspect" not in (rows["nosal"] or "")


def test_m081_backfill_is_idempotent(tmp_db_path):
    _pre_m081_db(tmp_db_path)
    _apply(tmp_db_path)
    _apply(tmp_db_path)  # re-run
    conn = sqlite3.connect(tmp_db_path)
    try:
        reasons = conn.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key='low'"
        ).fetchone()[0]
    finally:
        conn.close()
    # exactly one occurrence, not doubled
    assert reasons.count("salary_unit_suspect") == 1


def test_m081_check_rejects_bad_currency(tmp_db_path):
    _pre_m081_db(tmp_db_path)
    _apply(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (dedup_key, salary_currency) VALUES ('x', 'XYZ')")
    finally:
        conn.close()


def test_m081_check_rejects_bad_period(tmp_db_path):
    _pre_m081_db(tmp_db_path)
    _apply(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (dedup_key, salary_period) VALUES ('x', 'fortnightly')")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Greenhouse per-source emission
# ---------------------------------------------------------------------------


def test_greenhouse_emits_hourly_period_and_currency():
    posting = {
        "title": "Data Scientist",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "pay_input_ranges": [
            {"unit": "hour", "min_cents": 64, "max_cents": 90, "currency_type": "USD"}
        ],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "hourly"
    assert out["salary_currency"] == "USD"
    assert out["salary_min"] == 64


def test_greenhouse_emits_annual_eur():
    posting = {
        "title": "Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
        "pay_input_ranges": [
            {
                "interval": "year",
                "min_cents": 12_000_000,
                "max_cents": 18_000_000,
                "currency": "EUR",
            }
        ],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "annual"
    assert out["salary_currency"] == "EUR"
    assert out["salary_min"] == 120_000  # cents → dollars for year


def test_greenhouse_defaults_when_no_pay_ranges():
    posting = {"title": "Analyst", "absolute_url": "https://x/y"}
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "unknown"
    assert out["salary_currency"] == "USD"


def test_greenhouse_unknown_currency_falls_back_to_usd():
    posting = {
        "title": "X",
        "absolute_url": "https://x/y",
        "pay_input_ranges": [{"unit": "hour", "min_cents": 50, "currency": "XYZ"}],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_currency"] == "USD"


# ---------------------------------------------------------------------------
# End-to-end plumbing — Job → ParsedJob → upsert_job
# ---------------------------------------------------------------------------


def test_parsed_job_carries_currency_period_from_job():
    job = Job(
        title="Data Scientist",
        company="Acme",
        location="Remote",
        source="greenhouse",
        source_url="https://acme.com/1",
        salary_min=64,
        salary_max=90,
        salary_currency="EUR",
        salary_period="hourly",
    )
    parsed = ParsedJob.from_job(job)
    assert isinstance(parsed, ParsedJob)
    assert parsed.salary_currency == "EUR"
    assert parsed.salary_period == "hourly"


def test_upsert_writes_currency_period(migrated_db):
    from job_finder.db import upsert_job

    path, conn = migrated_db
    conn.row_factory = sqlite3.Row
    job = Job(
        title="Staff Data Scientist",
        company="Acme",
        location="Remote",
        source="greenhouse",
        source_url="https://acme.com/42",
        salary_min=180000,
        salary_max=240000,
        salary_currency="GBP",
        salary_period="annual",
    )
    result = upsert_job(conn, ParsedJob.from_job(job))
    assert result.kind == "inserted"
    row = conn.execute(
        "SELECT salary_currency, salary_period FROM jobs WHERE dedup_key = ?",
        (result.dedup_key,),
    ).fetchone()
    assert row["salary_currency"] == "GBP"
    assert row["salary_period"] == "annual"
