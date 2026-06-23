"""Tests for company_dedup.rewrite_loser_jobs dedup_key derivation.

rewrite_loser_jobs must build the rewritten dedup_key via the canonical
``derive_dedup_key`` (legal-suffix + abbreviation + level-suffix normalization),
NOT a raw ``.lower().strip()``. Otherwise the rewritten key diverges from the
key the ingest path computes, the in-function collision check misses, and a
duplicate jobs row is created instead of being deduped.
"""

import sqlite3

from job_finder.normalizers import derive_dedup_key
from job_finder.web.company_dedup import rewrite_loser_jobs


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY, title TEXT, "
        "company_id INTEGER, company TEXT)"
    )
    return conn


def _insert(conn, key, title, company_id, company):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company_id, company) VALUES (?, ?, ?, ?)",
        (key, title, company_id, company),
    )


def test_rewritten_key_collides_with_canonical_and_dedups():
    """Loser rewrites to the canonical key (suffixes normalized) -> deleted, not duplicated."""
    conn = _conn()
    canonical_name = "Acme Inc"
    title = "Senior Data Scientist (IC5)"
    canonical_key = derive_dedup_key(canonical_name, title)

    # Canonical company (id=1) already holds the job under the ingest key.
    _insert(conn, canonical_key, title, 1, canonical_name)
    # Loser company (id=2) holds the SAME real job under a divergent stored key.
    _insert(conn, "loserco|" + title.lower(), title, 2, "LoserCo")

    moved, deleted = rewrite_loser_jobs(
        conn, loser_id=2, canonical_id=1, canonical_name=canonical_name
    )

    # Raw .lower().strip() would have produced "acme inc|senior data scientist (ic5)"
    # (no collision) -> a divergent duplicate. derive_dedup_key collides -> dedup.
    assert (moved, deleted) == (0, 1)
    rows = conn.execute("SELECT dedup_key FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == canonical_key
    conn.close()


def test_rewritten_key_uses_derive_dedup_key_when_moving():
    """With no collision, the loser is moved under the canonical-derived key."""
    conn = _conn()
    canonical_name = "Acme Inc"
    title = "Staff Engineer (IC5)"
    _insert(conn, "loserco|staff engineer (ic5)", title, 2, "LoserCo")

    moved, deleted = rewrite_loser_jobs(
        conn, loser_id=2, canonical_id=1, canonical_name=canonical_name
    )

    assert (moved, deleted) == (1, 0)
    row = conn.execute("SELECT dedup_key, company_id, company FROM jobs").fetchone()
    assert row["dedup_key"] == derive_dedup_key(canonical_name, title)
    assert row["dedup_key"] == "acme|staff engineer"
    assert row["company_id"] == 1
    assert row["company"] == canonical_name
    conn.close()
