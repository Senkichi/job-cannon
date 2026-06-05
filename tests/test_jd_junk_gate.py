"""Tests for the set_jd_full() content-density gate (Phase 46.03).

Verifies:
  1. Each documented junk prefix causes set_jd_full() to return False and
     leaves jd_full unchanged in the DB.
  2. The length-floor case (text shorter than 200 chars) is also rejected.
  3. A legitimate long JD (≥200 chars, non-junk prefix) returns True and is
     written to the DB.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md §10
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator

import pytest

from job_finder.db._jd_full import set_jd_full
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")


def _make_migrated_db() -> tuple[str, sqlite3.Connection]:
    """Return (path, conn) for a temp DB with all migrations applied."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_job(conn: sqlite3.Connection, dedup_key: str) -> None:
    """Insert a minimal job row with jd_full = NULL."""
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                first_seen, last_seen, score, score_breakdown, locations_raw,
                unresolved_reasons)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), 0, '{}', '[]', '[]')""",
        (dedup_key, "Test Job", "TestCo", "", '["test"]', "[]"),
    )
    conn.commit()


def _read_jd(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    row = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row["jd_full"] if row else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Iterator[tuple[str, sqlite3.Connection]]:
    """Yield (path, conn) for a migrated temp DB; clean up after."""
    path, conn = _make_migrated_db()
    try:
        yield path, conn
    finally:
        conn.close()
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Junk-prefix cases — each should return False, leave jd_full unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Each documented junk prefix, padded to > 200 chars so only the prefix
        # triggers the gate (not the length floor).
        "Sign in to view the full job description" + " x" * 100,
        "Loading..." + " x" * 100,
        "Open roles at Acme Corp — join our team" + " x" * 100,
        "Skip to content\n\nMain content area" + " x" * 100,
        "Cookie Policy\nWe use cookies to improve your experience." + " x" * 100,
        "Privacy Policy\nYour privacy matters to us." + " x" * 100,
        "404 Not Found\nThe page you requested does not exist." + " x" * 100,
    ],
    ids=[
        "sign_in_to_view",
        "loading",
        "open_roles_at_acme",
        "skip_to_content",
        "cookie_policy",
        "privacy_policy",
        "404_not_found",
    ],
)
def test_junk_prefix_rejected(db, text):
    _, conn = db
    dedup_key = "test|junk_prefix"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, text, source="test")

    assert result is False, "set_jd_full should return False for junk prefix"
    assert _read_jd(conn, dedup_key) is None, "jd_full should remain NULL after junk-gated write"


def test_length_floor_rejected(db):
    """A short text (< 200 chars) should be rejected."""
    _, conn = db
    dedup_key = "test|length_floor"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, "Short.", source="test")

    assert result is False, "set_jd_full should return False for short text"
    assert _read_jd(conn, dedup_key) is None, (
        "jd_full should remain NULL after length-floor rejection"
    )


# ---------------------------------------------------------------------------
# Legitimate long JD — should return True and write
# ---------------------------------------------------------------------------


def test_legitimate_jd_written(db):
    """A long, non-junk JD should be written and set_jd_full should return True."""
    _, conn = db
    dedup_key = "test|legitimate_jd"
    _insert_job(conn, dedup_key)

    # Build a ≥200-char JD with a non-junk prefix
    long_jd = (
        "We are seeking a talented Software Engineer to join our growing team. "
        "You will work on distributed systems, mentor junior engineers, and "
        "collaborate with product managers to deliver high-impact features. "
        "Requirements: 5+ years Python, strong system design skills, BS/MS CS."
    )
    assert len(long_jd) >= 200, "test setup: long_jd must be ≥200 chars"

    result = set_jd_full(conn, dedup_key, long_jd, source="test")

    assert result is True, "set_jd_full should return True for a legitimate JD"
    stored = _read_jd(conn, dedup_key)
    assert stored == long_jd, "jd_full should match the written text"


def test_none_text_rejected(db):
    """Passing None should return False without touching the DB."""
    _, conn = db
    dedup_key = "test|none_text"
    _insert_job(conn, dedup_key)

    result = set_jd_full(conn, dedup_key, None, source="test")

    assert result is False
    assert _read_jd(conn, dedup_key) is None
