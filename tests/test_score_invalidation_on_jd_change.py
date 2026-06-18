"""Tests for the two-stage-leak fix (#226): score invalidation on jd_full change.

The v3.0 pipeline scores asynchronously — ingestion fills metadata, enrichment
fills ``jd_full`` later, and a Stage-2 sweep scores rows where
``classification IS NULL AND jd_full IS NOT NULL``. The leak: a row scored on
thin input (e.g. ``low_signal``) keeps its stale ``classification`` after
enrichment fills a real ``jd_full``, so the sweep never re-queues it.

The fix enforces a single invalidation invariant at the SOLE sanctioned
``jd_full`` writer (``set_jd_full``): when the stored ``jd_full`` materially
changes, the scoring tuple is cleared via ``invalidate_job_score`` (the scoring-
column singleton in ``_assessment_writer.py``), re-enrolling the row into the
existing sweep. Idempotent re-writes (same text) — the trivial re-sight case —
must NOT invalidate.

Verifies:
  1. jd_full NULL→set on a previously-scored row clears the scoring tuple
     (re-queued by the ``classification IS NULL`` sweep).
  2. A materially changed jd_full on a scored row clears the scoring tuple.
  3. Writing the SAME jd_full again (idempotent re-fetch) leaves the score
     intact (no churn on trivial re-sights).
  4. A junk-gated write never invalidates (no write, no invalidation).
  5. ``invalidate_job_score`` clears all five scoring columns atomically and
     returns the row-matched boolean.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator

import pytest

from job_finder.db import invalidate_job_score
from job_finder.db._jd_full import set_jd_full
from job_finder.web.db_migrate import run_migrations

os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")

# A clean, ≥200-char, non-junk JD body.
_LONG_JD_A = (
    "We are seeking a Senior Backend Engineer to design and operate distributed "
    "systems at scale. You will own services end to end, mentor engineers, and "
    "partner with product to ship high-impact features. Requirements: 6+ years "
    "Python, strong system design, and a track record of operational excellence."
)
_LONG_JD_B = (
    "Join our platform team as a Staff Software Engineer. You will lead "
    "architecture for our data ingestion pipeline, drive reliability "
    "initiatives, and coach senior engineers. Requirements: 8+ years building "
    "back-end systems, deep SQL knowledge, and experience with event streaming."
)


def _make_migrated_db() -> tuple[str, sqlite3.Connection]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_scored_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    *,
    jd_full: str | None,
    classification: str = "low_signal",
) -> None:
    """Insert a job carrying a full scoring tuple.

    Mirrors the on-disk shape persist_job_assessment leaves: classification +
    sub_scores_json + fit_analysis + scoring_provider + scoring_model all set
    together (the m078 I-04/I-05 triggers require this coherent shape).
    """
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                first_seen, last_seen, score, score_breakdown, locations_raw,
                unresolved_reasons, jd_full,
                classification, sub_scores_json, fit_analysis,
                scoring_provider, scoring_model)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'),
                   0, '{}', '[]', '[]', ?,
                   ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Test Job",
            "TestCo",
            "",
            '["test"]',
            "[]",
            jd_full,
            classification,
            '{"overall_fit": 2}',
            '["thin input"]',
            "ollama",
            "qwen2.5:14b",
        ),
    )
    conn.commit()


def _read_scoring_tuple(conn: sqlite3.Connection, dedup_key: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT classification, sub_scores_json, fit_analysis, "
        "scoring_provider, scoring_model, jd_full "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()


@pytest.fixture()
def db() -> Iterator[sqlite3.Connection]:
    path, conn = _make_migrated_db()
    try:
        yield conn
    finally:
        conn.close()
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Re-queue cases: jd_full transitions → scoring tuple cleared
# ---------------------------------------------------------------------------


def test_jd_null_to_set_invalidates_score(db):
    """jd_full NULL→set on a scored row clears classification (re-queued)."""
    conn = db
    dedup_key = "test|null_to_set"
    _insert_scored_job(conn, dedup_key, jd_full=None, classification="low_signal")

    written = set_jd_full(conn, dedup_key, _LONG_JD_A, source="test")
    assert written is True

    row = _read_scoring_tuple(conn, dedup_key)
    assert row["jd_full"] == _LONG_JD_A
    assert row["classification"] is None, "classification must clear so the sweep re-queues it"
    assert row["sub_scores_json"] is None
    assert row["fit_analysis"] is None
    assert row["scoring_model"] is None
    # scoring_provider is tied to the heuristic `score` (I-03) and re-stamped on
    # the next score — left intact by design, not part of the cleared LLM tuple.


def test_jd_materially_changed_invalidates_score(db):
    """A different jd_full body on a scored row clears the scoring tuple."""
    conn = db
    dedup_key = "test|changed"
    _insert_scored_job(conn, dedup_key, jd_full=_LONG_JD_A, classification="consider")

    written = set_jd_full(conn, dedup_key, _LONG_JD_B, source="test")
    assert written is True

    row = _read_scoring_tuple(conn, dedup_key)
    assert row["jd_full"] == _LONG_JD_B
    assert row["classification"] is None
    assert row["scoring_model"] is None


# ---------------------------------------------------------------------------
# No-churn case: identical jd_full re-write → score preserved
# ---------------------------------------------------------------------------


def test_identical_jd_rewrite_preserves_score(db):
    """Re-writing the SAME jd_full (idempotent re-sight) must NOT invalidate."""
    conn = db
    dedup_key = "test|idempotent"
    _insert_scored_job(conn, dedup_key, jd_full=_LONG_JD_A, classification="apply")

    written = set_jd_full(conn, dedup_key, _LONG_JD_A, source="test")
    assert written is True, "identical write still returns True (write path runs)"

    row = _read_scoring_tuple(conn, dedup_key)
    assert row["jd_full"] == _LONG_JD_A
    assert row["classification"] == "apply", "trivial re-sight must not churn the score"
    assert row["scoring_model"] == "qwen2.5:14b"
    assert row["scoring_provider"] == "ollama"


def test_junk_write_does_not_invalidate(db):
    """A junk-gated write performs no write and no invalidation."""
    conn = db
    dedup_key = "test|junk"
    _insert_scored_job(conn, dedup_key, jd_full=_LONG_JD_A, classification="apply")

    written = set_jd_full(conn, dedup_key, "Short.", source="test")
    assert written is False

    row = _read_scoring_tuple(conn, dedup_key)
    assert row["jd_full"] == _LONG_JD_A, "junk write must not overwrite stored jd_full"
    assert row["classification"] == "apply", "junk write must not invalidate the score"


# ---------------------------------------------------------------------------
# invalidate_job_score unit behavior
# ---------------------------------------------------------------------------


def test_invalidate_job_score_clears_all_columns(db):
    """invalidate_job_score nulls every scoring column and reports a match."""
    conn = db
    dedup_key = "test|direct_invalidate"
    _insert_scored_job(conn, dedup_key, jd_full=_LONG_JD_A, classification="consider")

    matched = invalidate_job_score(conn, dedup_key)
    assert matched is True

    row = _read_scoring_tuple(conn, dedup_key)
    assert row["classification"] is None
    assert row["sub_scores_json"] is None
    assert row["fit_analysis"] is None
    assert row["scoring_model"] is None
    # scoring_provider is preserved by design (I-03: required while `score` set).
    assert row["scoring_provider"] == "ollama"
    # jd_full is scoring-input, not a scoring-owned column — left untouched.
    assert row["jd_full"] == _LONG_JD_A


def test_invalidate_job_score_missing_row_returns_false(db):
    """No matching dedup_key → returns False (SQLite UPDATE-no-match)."""
    conn = db
    assert invalidate_job_score(conn, "test|does_not_exist") is False
