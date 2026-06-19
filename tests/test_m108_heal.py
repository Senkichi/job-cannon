"""Tests for Migration 108 — deterministic location sync + quarantine (P2.4).

Exercises the four outcome classes of m108 against a fully-migrated schema
(``migrated_db``), the I-07 guard, and idempotency. No LLM / network — the
migration is offline-deterministic by contract.
"""

from __future__ import annotations

import json
import sqlite3

from job_finder.web.migrations.m108_location_sync import MIGRATION
from job_finder.web.migrations.types import MigrationContext


def _run(db_path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=".", initial_version=107)
    MIGRATION.py(ctx)  # type: ignore[misc]
    conn.commit()


def _insert(conn: sqlite3.Connection, dedup_key: str, **cols) -> None:
    cols.setdefault("title", "Data Scientist")
    cols.setdefault("company", "Acme")
    cols.setdefault("first_seen", "2026-06-01T00:00:00")
    cols.setdefault("last_seen", "2026-06-01T00:00:00")
    cols.setdefault("sources", "[]")
    cols.setdefault("source_urls", "[]")
    cols.setdefault("unresolved_reasons", "[]")
    columns = ", ".join(["dedup_key", *cols.keys()])
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    conn.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
        (dedup_key, *cols.values()),
    )
    conn.commit()


def _get(conn: sqlite3.Connection, dedup_key: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()


# ---------------------------------------------------------------------------
# Class 1 — legacy side-door write: location non-empty, locations_raw empty
# ---------------------------------------------------------------------------


def test_class1_seeds_locations_raw_and_structured(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "c1", location="Hyderabad", locations_raw="[]", locations_structured=None)
    _run(db_path, conn)
    row = _get(conn, "c1")
    assert json.loads(row["locations_raw"]) == ["Hyderabad"]
    structured = json.loads(row["locations_structured"])
    assert structured and not structured[0]["unresolved"]
    assert row["location"] == "Hyderabad"
    assert row["primary_country_code"] is not None


def test_class1_handles_null_locations_raw(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "c1n", location="Berlin", locations_raw=None, locations_structured=None)
    _run(db_path, conn)
    row = _get(conn, "c1n")
    assert json.loads(row["locations_raw"]) == ["Berlin"]
    assert json.loads(row["locations_structured"])


# ---------------------------------------------------------------------------
# Class 2 — locations_raw non-empty, locations_structured NULL (m067 skip/drift)
# ---------------------------------------------------------------------------


def test_class2_backfills_structured_preserving_raw_and_location(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c2",
        location="London, England",
        locations_raw=json.dumps(["London"]),
        locations_structured=None,
    )
    _run(db_path, conn)
    row = _get(conn, "c2")
    # locations_raw and the display string are preserved (m067 verbatim).
    assert json.loads(row["locations_raw"]) == ["London"]
    assert row["location"] == "London, England"
    structured = json.loads(row["locations_structured"])
    assert structured and not structured[0]["unresolved"]


# ---------------------------------------------------------------------------
# Class 3 — careers_crawl all-empty, recover from URL slug
# ---------------------------------------------------------------------------


def test_class3_recovers_location_from_url_slug(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3",
        location="",
        locations_raw="[]",
        locations_structured=None,
        sources=json.dumps(["careers_crawl"]),
        source_urls=json.dumps(["https://careers.example.com/jobs/data-scientist/hyderabad"]),
    )
    _run(db_path, conn)
    row = _get(conn, "c3")
    # The display string is the raw slug-derived candidate (matches the funnel's
    # ", ".join(locations_raw) semantics); the canonical city is gazetteer-cased.
    assert row["location"].lower() == "hyderabad"
    structured = json.loads(row["locations_structured"])
    assert structured and structured[0]["country_code"] == "IN"
    assert not structured[0]["unresolved"]


def test_class3_skips_non_careers_crawl_rows(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3n",
        location="",
        locations_raw="[]",
        sources=json.dumps(["serpapi"]),
        source_urls=json.dumps(["https://careers.example.com/jobs/hyderabad"]),
    )
    _run(db_path, conn)
    row = _get(conn, "c3n")
    # Not careers_crawl → class 3 leaves it; no jd_full so class 4 leaves it too.
    assert row["location"] == ""


def test_class3_no_resolvable_slug_leaves_row_empty(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3e",
        location="",
        locations_raw="[]",
        sources=json.dumps(["careers_crawl"]),
        source_urls=json.dumps(["https://careers.example.com/jobs/12345"]),
    )
    _run(db_path, conn)
    assert _get(conn, "c3e")["location"] == ""


# ---------------------------------------------------------------------------
# Class 4 — quarantine the residue: empty location + jd_full >= 200 chars
# ---------------------------------------------------------------------------


def test_class4_tags_location_missing_on_substantive_jd(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "c4", location="", locations_raw="[]", jd_full="x" * 250)
    _run(db_path, conn)
    reasons = json.loads(_get(conn, "c4")["unresolved_reasons"])
    assert "location_missing" in reasons


def test_class4_skips_empty_jd(migrated_db):
    # jd_full < 200 chars can't exist (I-13 blocks the write), so the only
    # "insufficient evidence" state is a NULL jd_full — class 4 must skip it.
    db_path, conn = migrated_db
    _insert(conn, "c4s", location="", locations_raw="[]", jd_full=None)
    _run(db_path, conn)
    assert json.loads(_get(conn, "c4s")["unresolved_reasons"]) == []


def test_class4_does_not_duplicate_existing_tag(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c4d",
        location="",
        locations_raw="[]",
        jd_full="x" * 250,
        unresolved_reasons=json.dumps(["location_missing"]),
    )
    _run(db_path, conn)
    assert json.loads(_get(conn, "c4d")["unresolved_reasons"]) == ["location_missing"]


def test_class4_preserves_other_reasons(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "c4p",
        location="",
        locations_raw="[]",
        jd_full="x" * 250,
        unresolved_reasons=json.dumps(["jd_full_junk"]),
    )
    _run(db_path, conn)
    reasons = json.loads(_get(conn, "c4p")["unresolved_reasons"])
    assert set(reasons) == {"jd_full_junk", "location_missing"}


def test_class3_recovered_row_not_quarantined(migrated_db):
    """A class-3 recovery fills location, so class 4 must not also tag it."""
    db_path, conn = migrated_db
    _insert(
        conn,
        "c3q",
        location="",
        locations_raw="[]",
        jd_full="y" * 300,
        sources=json.dumps(["careers_crawl"]),
        source_urls=json.dumps(["https://careers.example.com/jobs/hyderabad"]),
    )
    _run(db_path, conn)
    row = _get(conn, "c3q")
    assert row["location"].lower() == "hyderabad"
    assert json.loads(row["unresolved_reasons"]) == []


# ---------------------------------------------------------------------------
# Idempotency + no-op safety
# ---------------------------------------------------------------------------


def test_idempotent_second_run_is_noop(migrated_db):
    db_path, conn = migrated_db
    _insert(conn, "i1", location="Hyderabad", locations_raw="[]")
    _insert(
        conn,
        "i2",
        location="",
        locations_raw="[]",
        jd_full="z" * 250,
        sources=json.dumps(["careers_crawl"]),
        source_urls=json.dumps(["https://careers.example.com/jobs/hyderabad"]),
    )
    _insert(conn, "i3", location="", locations_raw="[]", jd_full="z" * 250)
    _run(db_path, conn)
    snapshot = {k: dict(_get(conn, k)) for k in ("i1", "i2", "i3")}
    _run(db_path, conn)
    after = {k: dict(_get(conn, k)) for k in ("i1", "i2", "i3")}
    assert snapshot == after


def test_no_jobs_table_is_noop():
    conn = sqlite3.connect(":memory:")
    # No jobs table — the migration must not raise.
    _run(":memory:", conn)


def test_well_formed_rows_untouched(migrated_db):
    db_path, conn = migrated_db
    _insert(
        conn,
        "ok",
        location="Hyderabad",
        locations_raw=json.dumps(["Hyderabad"]),
        locations_structured=json.dumps(
            [
                {
                    "city": "Hyderabad",
                    "region": None,
                    "region_code": None,
                    "country": "India",
                    "country_code": "IN",
                    "workplace_type": "ONSITE",
                    "raw": "Hyderabad",
                    "unresolved": False,
                }
            ]
        ),
        unresolved_reasons="[]",
    )
    before = dict(_get(conn, "ok"))
    _run(db_path, conn)
    assert dict(_get(conn, "ok")) == before
