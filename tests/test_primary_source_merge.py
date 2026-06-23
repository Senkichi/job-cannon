"""Tests for merge_primary_posting_fields (strict-match authoritative merge)."""

from __future__ import annotations

import json
import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.primary_source_merge import merge_primary_posting_fields


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _seed(conn, **overrides) -> str:
    """Insert a company + one aggregator-sourced job row; return its dedup_key."""
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    row = {
        "dedup_key": "acme|senior data scientist",
        "title": "Senior Data Scientist",
        "company": "Acme",
        "location": "New York, NY",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-01-01T00:00:00",
        "sources": '["linkedin"]',
        "source_urls": '["https://www.linkedin.com/jobs/view/1"]',
        "company_id": 1,
        **overrides,
    }
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", tuple(row.values()))
    conn.commit()
    return row["dedup_key"]


def _posting(**overrides) -> dict:
    """A Lever-shaped strict-matched posting."""
    posting = {
        "title": "Senior Data Scientist",
        "company_source": "Lever",
        "location": "New York",
        "description": "Real ATS description. " + "x" * 300,
        "source_url": "https://jobs.lever.co/acme/abc",
        "salary_min": 150000,
        "salary_max": 190000,
        "source_id": "abc-123",
        "posted_date": "2026-05-01T00:00:00",
    }
    posting.update(overrides)
    return posting


def _row(conn, dedup_key) -> dict:
    return dict(conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone())


def test_merge_fills_salary_url_source_and_metadata(tmp_path):
    conn = _migrated_db(tmp_path)
    key = _seed(conn)

    assert merge_primary_posting_fields(conn, {"dedup_key": key}, _posting()) is True

    row = _row(conn, key)
    assert row["salary_min"] == 150000
    assert row["salary_max"] == 190000
    assert row["posted_date"] == "2026-05-01T00:00:00"
    assert "https://jobs.lever.co/acme/abc" in json.loads(row["source_urls"])
    assert "https://www.linkedin.com/jobs/view/1" in json.loads(row["source_urls"])
    assert json.loads(row["sources"]) == ["linkedin", "Lever"]
    assert row["source_id"] == "abc-123"
    # description merged + eagerly promoted through the I-13 gate
    assert row["jd_full"] and row["jd_full"].startswith("Real ATS description.")
    conn.close()


def test_merge_salary_trust_ranked_overwrite(tmp_path):
    """P1.5 (D-4): the mirrored "first-seen salary wins" suppression is DELETED.

    The primary posting is a strict-matched ATS source (provenance
    'ats_structured', rank 4); it now overwrites a stored legacy/unranked pair
    (NULL provenance → rank 0) through trust-ranked, pair-atomic reconciliation.
    Previously asserted the opposite; updated per issue #381.
    """
    conn = _migrated_db(tmp_path)
    key = _seed(conn, salary_min=120000, salary_max=140000)

    merge_primary_posting_fields(conn, {"dedup_key": key}, _posting())

    row = _row(conn, key)
    assert row["salary_min"] == 150000
    assert row["salary_max"] == 190000
    assert row["salary_provenance"] == "ats_structured"
    conn.close()


def test_merge_exact_posted_date_corrects_proxy(tmp_path):
    """An ATS first-posted timestamp overwrites a stored email-proxy date (#363)."""
    conn = _migrated_db(tmp_path)
    key = _seed(conn, posted_date="2026-01-15T00:00:00", posted_date_precision="proxy")

    merge_primary_posting_fields(conn, {"dedup_key": key}, _posting())

    row = _row(conn, key)
    assert row["posted_date"] == "2026-05-01T00:00:00"
    assert row["posted_date_precision"] == "exact"
    conn.close()


def test_merge_posted_date_never_churns_equal_precision(tmp_path):
    """A stored exact date is kept against another exact sighting (stability)."""
    conn = _migrated_db(tmp_path)
    key = _seed(conn, posted_date="2026-01-15T00:00:00", posted_date_precision="exact")

    merge_primary_posting_fields(conn, {"dedup_key": key}, _posting())

    assert _row(conn, key)["posted_date"] == "2026-01-15T00:00:00"
    conn.close()


def test_merge_pins_identity_despite_title_drift(tmp_path):
    """An ATS title that normalizes to a different dedup_key must NOT mint a
    second row — the merge is pinned to the existing row's identity."""
    conn = _migrated_db(tmp_path)
    key = _seed(conn)

    merge_primary_posting_fields(
        conn, {"dedup_key": key}, _posting(title="Sr. Data Scientist II (NYC)")
    )

    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1
    row = _row(conn, key)
    assert row["title"] == "Senior Data Scientist"
    assert "https://jobs.lever.co/acme/abc" in json.loads(row["source_urls"])
    conn.close()


def test_merge_source_id_conflict_is_skipped(tmp_path):
    """I-11: when another row already holds (company_id, source_id), the
    source_id write is skipped (drifted-title twin), never raised."""
    conn = _migrated_db(tmp_path)
    key = _seed(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "company_id, source_id) VALUES "
        "('acme|sr data scientist ii', 'Sr Data Scientist II', 'Acme', 'Remote', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 1, 'abc-123')"
    )
    conn.commit()

    merge_primary_posting_fields(conn, {"dedup_key": key}, _posting())

    # Column default is '' (not NULL) — assert the conflicting id was NOT written.
    assert not _row(conn, key)["source_id"]
    holder = conn.execute("SELECT dedup_key FROM jobs WHERE source_id = 'abc-123'").fetchone()
    assert holder["dedup_key"] == "acme|sr data scientist ii"
    conn.close()


def test_merge_preserves_unresolved_reasons(tmp_path):
    # jobs.score was dropped in m113; the score-preservation half of this
    # contract is retired. The unresolved_reasons preservation contract remains.
    conn = _migrated_db(tmp_path)
    key = _seed(
        conn,
        unresolved_reasons='["title_metadata_blob"]',
    )

    merge_primary_posting_fields(conn, {"dedup_key": key}, _posting())

    row = _row(conn, key)
    assert json.loads(row["unresolved_reasons"]) == ["title_metadata_blob"]
    conn.close()


def test_merge_missing_row_or_posting_is_noop(tmp_path):
    conn = _migrated_db(tmp_path)
    assert merge_primary_posting_fields(conn, {"dedup_key": "nope"}, _posting()) is False
    assert merge_primary_posting_fields(conn, {"dedup_key": ""}, _posting()) is False
    assert merge_primary_posting_fields(conn, {"dedup_key": "x"}, {}) is False
    conn.close()
