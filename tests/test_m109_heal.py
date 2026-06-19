"""Tests for Migration 109 — heal salary units from retained evidence + ceiling tripwire (P1.7).

Exercises the four-class healing ladder against a fully-migrated schema
(``migrated_db``), the I-16 ceiling tripwire, the healthy-row-untouched guarantee,
and idempotency. Offline-deterministic by contract — no LLM, no network.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from job_finder.web.migrations.m109_heal_salary_units import MIGRATION
from job_finder.web.migrations.types import MigrationContext


def _run(db_path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=".", initial_version=108)
    MIGRATION.py(ctx)  # type: ignore[misc]
    conn.commit()


def _insert(conn: sqlite3.Connection, dedup_key: str, **cols) -> None:
    cols.setdefault("title", "Data Scientist")
    cols.setdefault("company", "Acme")
    cols.setdefault("location", "Remote")
    cols.setdefault("first_seen", "2026-06-01T00:00:00")
    cols.setdefault("last_seen", "2026-06-01T00:00:00")
    cols.setdefault("sources", "[]")
    cols.setdefault("source_urls", "[]")
    cols.setdefault("unresolved_reasons", "[]")
    cols.setdefault("salary_observations", "[]")
    columns = ", ".join(["dedup_key", *cols.keys()])
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    conn.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
        (dedup_key, *cols.values()),
    )
    conn.commit()


def _disarm(conn: sqlite3.Connection) -> None:
    """Drop the I-16 ceiling tripwire so corrupt (>$5M) rows can be seeded.

    ``migrated_db`` has already applied m109 (auto-discovered), so the tripwire is
    armed in the template. Dropping it reproduces the pre-m109 dirty-DB state; the
    subsequent ``_run`` re-heals AND re-arms the trigger.
    """
    conn.execute("DROP TRIGGER IF EXISTS tg_jobs_salary_max_ceiling_ins")
    conn.execute("DROP TRIGGER IF EXISTS tg_jobs_salary_max_ceiling_upd")
    conn.commit()


def _get(conn: sqlite3.Connection, dedup_key: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()


# A jd_full long enough to clear the I-13 content-density floor (>= 200 chars).
def _jd_with_range(range_text: str) -> str:
    filler = (
        " We are seeking an experienced practitioner to join our growing data team "
        "and partner closely with product and engineering on impactful problems. "
        "Responsibilities include analysis, modeling, and stakeholder communication."
    )
    return f"Compensation: {range_text}.{filler}"


# ---------------------------------------------------------------------------
# Class 1 — Greenhouse comp_data_json structured evidence
# ---------------------------------------------------------------------------


def test_class1_unitless_cents_salvaged_from_comp_json(migrated_db):
    """Northbeam case: raw unit-less cents salvage to $170k via the cents rung."""
    db_path, conn = migrated_db
    _disarm(conn)
    _insert(
        conn,
        "c1cents",
        salary_min=17_000_000,
        salary_max=20_000_000,
        comp_data_json=json.dumps([{"min_cents": 17_000_000, "max_cents": 20_000_000}]),
    )
    _run(db_path, conn)
    row = _get(conn, "c1cents")
    assert row["salary_min"] == 170_000
    assert row["salary_max"] == 200_000
    assert row["salary_provenance"] == "ats_structured"
    obs = json.loads(row["salary_observations"])
    assert obs and obs[-1]["provenance"] == "ats_structured"
    assert obs[-1]["resolution"] == "salvaged_cents"


def test_class1_year_interval_cents_decoded_to_annual(migrated_db):
    """unit='year' cents decode via the P1.3 ÷100 rung (rung 1), not the cents rung."""
    db_path, conn = migrated_db
    _disarm(conn)
    _insert(
        conn,
        "c1year",
        salary_min=17_000_000,
        salary_max=20_000_000,
        comp_data_json=json.dumps(
            [{"min_cents": 17_000_000, "max_cents": 20_000_000, "unit": "year"}]
        ),
    )
    _run(db_path, conn)
    row = _get(conn, "c1year")
    assert row["salary_min"] == 170_000
    assert row["salary_max"] == 200_000
    assert row["salary_provenance"] == "ats_structured"
    assert row["salary_period"] == "annual"


# ---------------------------------------------------------------------------
# Class 3 — jd_full re-extraction
# ---------------------------------------------------------------------------


def test_class3_hourly_min_reextracted_from_jd(migrated_db):
    """Bare hourly 46/None does NOT salvage in class 2; jd_full hourly range → class 3."""
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3hourly",
        salary_min=46,
        salary_max=None,
        jd_full=_jd_with_range("the hourly pay range for this position is $40 - $50 an hour"),
    )
    _run(db_path, conn)
    row = _get(conn, "c3hourly")
    assert row["salary_min"] == 40 * 2080
    assert row["salary_max"] == 50 * 2080
    assert row["salary_provenance"] == "jd_regex"
    obs = json.loads(row["salary_observations"])
    assert obs and obs[-1]["provenance"] == "jd_regex"


def test_class3_franken_pair_with_jd_reextracted(migrated_db):
    """3k-Franken min + plausible max, jd_full present → re-extracted (class 3)."""
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3frank",
        salary_min=3000,
        salary_max=251_000,
        jd_full=_jd_with_range("the base salary range is $180,000 - $220,000"),
    )
    _run(db_path, conn)
    row = _get(conn, "c3frank")
    assert row["salary_min"] == 180_000
    assert row["salary_max"] == 220_000
    assert row["salary_provenance"] == "jd_regex"


# ---------------------------------------------------------------------------
# Class 4 — quarantine
# ---------------------------------------------------------------------------


def test_class4_bare_cents_pair_quarantined(migrated_db):
    """Bare cents pair, no comp_json, no jd_full → NULL + salary_implausible + retained."""
    db_path, conn = migrated_db
    _disarm(conn)
    _insert(conn, "c4cents", salary_min=17_000_000, salary_max=20_000_000)
    _run(db_path, conn)
    row = _get(conn, "c4cents")
    assert row["salary_min"] is None
    assert row["salary_max"] is None
    assert "salary_implausible" in json.loads(row["unresolved_reasons"])
    legacy = json.loads(row["salary_observations"])[-1]
    assert legacy["provenance"] == "legacy"
    assert legacy["min_value"] == 17_000_000
    assert legacy["raw_text"] == "pre-m109 columns"


def test_class4_franken_pair_without_jd_quarantined(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "c4frank", salary_min=3000, salary_max=251_000)
    _run(db_path, conn)
    row = _get(conn, "c4frank")
    assert row["salary_min"] is None and row["salary_max"] is None
    assert "salary_implausible" in json.loads(row["unresolved_reasons"])


def test_class4_preserves_other_unresolved_reasons(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c4keep",
        salary_min=46,
        salary_max=None,
        unresolved_reasons=json.dumps(["jd_full_junk"]),
    )
    _run(db_path, conn)
    reasons = set(json.loads(_get(conn, "c4keep")["unresolved_reasons"]))
    assert reasons == {"jd_full_junk", "salary_implausible"}


# ---------------------------------------------------------------------------
# Healthy rows untouched
# ---------------------------------------------------------------------------


def test_healthy_in_bounds_row_untouched(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "ok", salary_min=170_000, salary_max=200_000)
    before = dict(_get(conn, "ok"))
    _run(db_path, conn)
    assert dict(_get(conn, "ok")) == before


def test_healthy_single_sided_row_untouched(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "ok1", salary_min=120_000, salary_max=None)
    before = dict(_get(conn, "ok1"))
    _run(db_path, conn)
    assert dict(_get(conn, "ok1")) == before


def test_null_salary_row_untouched(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "nosal", salary_min=None, salary_max=None)
    before = dict(_get(conn, "nosal"))
    _run(db_path, conn)
    assert dict(_get(conn, "nosal")) == before


# ---------------------------------------------------------------------------
# I-16 ceiling tripwire
# ---------------------------------------------------------------------------


def test_tripwire_blocks_insert_above_ceiling(migrated_db):
    db_path, conn = migrated_db
    _run(db_path, conn)
    with pytest.raises(sqlite3.IntegrityError, match="I-16"):
        _insert(conn, "over", salary_min=100_000, salary_max=6_000_000)


def test_tripwire_blocks_update_above_ceiling(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "clean", salary_min=170_000, salary_max=200_000)
    _run(db_path, conn)
    with pytest.raises(sqlite3.IntegrityError, match="I-16"):
        conn.execute("UPDATE jobs SET salary_max = 6000000 WHERE dedup_key = 'clean'")
        conn.commit()


def test_tripwire_allows_in_bounds_write(migrated_db):
    db_path, conn = migrated_db
    _run(db_path, conn)
    _insert(conn, "fine", salary_min=120_000, salary_max=180_000)
    assert _get(conn, "fine")["salary_max"] == 180_000


def test_phase_exit_criterion_no_corrupt_rows_remain(migrated_db):
    """After heal: no row has salary_min > 5M or a positive sub-floor salary_min."""
    db_path, conn = migrated_db
    _disarm(conn)
    _insert(conn, "x1", salary_min=17_000_000, salary_max=20_000_000)
    _insert(conn, "x2", salary_min=46, salary_max=None)
    _insert(conn, "x3", salary_min=3000, salary_max=251_000)
    _run(db_path, conn)
    bad = conn.execute(
        "SELECT count(*) FROM jobs WHERE salary_min > 5000000 "
        "OR (salary_min > 0 AND salary_min < 30000) OR salary_max > 5000000"
    ).fetchone()[0]
    assert bad == 0


# ---------------------------------------------------------------------------
# Idempotency + no-op safety
# ---------------------------------------------------------------------------


def test_idempotent_second_run_is_noop(migrated_db):
    db_path, conn = migrated_db
    _disarm(conn)
    _insert(
        conn,
        "i1",
        salary_min=17_000_000,
        salary_max=20_000_000,
        comp_data_json=json.dumps([{"min_cents": 17_000_000, "max_cents": 20_000_000}]),
    )
    _insert(conn, "i2", salary_min=17_000_000, salary_max=20_000_000)
    _insert(
        conn,
        "i3",
        salary_min=46,
        salary_max=None,
        jd_full=_jd_with_range("the hourly pay range for this position is $40 - $50 an hour"),
    )
    _insert(conn, "i4", salary_min=170_000, salary_max=200_000)
    _run(db_path, conn)
    snapshot = {k: dict(_get(conn, k)) for k in ("i1", "i2", "i3", "i4")}
    _run(db_path, conn)
    after = {k: dict(_get(conn, k)) for k in ("i1", "i2", "i3", "i4")}
    assert snapshot == after


def test_no_jobs_table_is_noop():
    conn = sqlite3.connect(":memory:")
    _run(":memory:", conn)
