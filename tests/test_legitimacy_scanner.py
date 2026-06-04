"""Tests for legitimacy_scanner.scan_legitimacy() and its orchestrator wiring.

Covers:
1. Scam-pattern JD → scan_legitimacy returns a non-None note.
2. Clean JD → scan_legitimacy returns None.
3. Empty / None-equivalent input → scan_legitimacy returns None.
4. Regex pattern (earn $NNN/day) is matched.
5. End-to-end: score_and_persist_job on a flagged JD produces
   legitimacy_note non-NULL AND classification='reject'.
6. End-to-end: score_and_persist_job on a clean JD leaves
   legitimacy_note NULL and classification determined by sub_scores.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.db import JobAssessment
from job_finder.web import scoring_orchestrator as so
from job_finder.web.db_migrate import run_migrations
from job_finder.web.job_scorer import ScoringResult
from job_finder.web.legitimacy_scanner import scan_legitimacy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCAM_JD = (
    "About this opportunity: we offer unlimited income potential for driven "
    "individuals who want financial freedom.  Join our growing team and be your "
    "own boss from day one.  No experience required — earnings depend on your "
    "hustle and your ability to recruit your downline."
)

_CLEAN_JD = (
    "Senior Data Engineer — Remote.  "
    "You will design, build, and maintain scalable data pipelines using Python "
    "and Spark.  Strong SQL skills required.  Competitive salary + equity.  "
    "5+ years experience in data engineering.  We value work-life balance and "
    "provide comprehensive health benefits."
)


def _make_assessment(all_score: int = 3):
    """Return a JobAssessment with all sub-scores set to ``all_score``."""
    sub_scores = {
        "title_fit": all_score,
        "location_fit": all_score,
        "comp_fit": all_score,
        "domain_match": all_score,
        "seniority_match": all_score,
        "skills_match": all_score,
    }
    rationale = {
        "strengths": ["ok"],
        "gaps": [],
        "talking_points": [],
        "resume_priority_skills": [],
    }
    return JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale=rationale,
        provider="ollama",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn():
    """Fully migrated in-memory-like DB (fresh temp file + run_migrations)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, path
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_job(conn: sqlite3.Connection, dedup_key: str, jd_full: str) -> dict:
    """Insert a minimal jobs row and return the job dict for the orchestrator."""
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, sources,
                          source_urls, source_id, first_seen, last_seen,
                          score, score_breakdown, user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            "Test Role",
            "ACME",
            "Remote",
            '["test"]',
            '["https://example.com/1"]',
            "src-1",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            0.0,
            "{}",
            "unreviewed",
            jd_full,
        ),
    )
    conn.commit()
    return {"dedup_key": dedup_key, "title": "Test Role", "company": "ACME",
            "location": "Remote", "jd_full": jd_full}


_BASE_CONFIG: dict = {}  # minimal config; scorer is stubbed anyway


# ---------------------------------------------------------------------------
# Unit tests — scan_legitimacy()
# ---------------------------------------------------------------------------


class TestScanLegitimacy:
    """Unit-level tests for the scan_legitimacy function."""

    def test_scam_jd_returns_note(self):
        """Acceptance criterion 1: scam-pattern JD → non-None note."""
        result = scan_legitimacy(_SCAM_JD)
        assert result is not None, "Expected a non-None note for scam JD"
        assert "suspicious_pattern" in result

    def test_clean_jd_returns_none(self):
        """Acceptance criterion 2: clean JD → None."""
        result = scan_legitimacy(_CLEAN_JD)
        assert result is None, f"Expected None for clean JD but got {result!r}"

    def test_empty_string_returns_none(self):
        """Empty input is safe and returns None."""
        assert scan_legitimacy("") is None

    def test_each_substring_pattern_fires(self):
        """Every configured substring pattern triggers the scanner."""
        patterns = [
            "unlimited income potential",
            "be your own boss",
            "work from home opportunity",
            "join our team of leaders",
            "financial freedom",
            "residual income",
            "recruit your downline",
            "earnings depend on",
            "crypto trading opportunity",
        ]
        for pat in patterns:
            note = scan_legitimacy(f"This role offers {pat} to all candidates.")
            assert note is not None, f"Pattern {pat!r} did not fire"
            assert "suspicious_pattern" in note

    def test_regex_pattern_earn_per_day(self):
        """Regex pattern 'earn $NNN/day' fires correctly."""
        note = scan_legitimacy("You can earn $500/day working from home!")
        assert note is not None
        assert "suspicious_pattern" in note

    def test_regex_short_amount_no_match(self):
        """Two-digit dollar amount does NOT trigger earn-per-day regex (conservative)."""
        note = scan_legitimacy("You can earn $50/day as a part-time cashier.")
        assert note is None

    def test_case_insensitive_match(self):
        """Pattern match is case-insensitive (text is lowercased before checking)."""
        note = scan_legitimacy("UNLIMITED INCOME POTENTIAL for everyone!")
        assert note is not None

    def test_returns_first_match_only(self):
        """Function returns a single note, not one per pattern."""
        multi_pattern = "unlimited income potential and financial freedom"
        note = scan_legitimacy(multi_pattern)
        assert note is not None
        assert isinstance(note, str)


# ---------------------------------------------------------------------------
# End-to-end tests — orchestrator wiring
# ---------------------------------------------------------------------------


class TestOrchestratorLegitimacyWiring:
    """End-to-end: score_and_persist_job writes legitimacy_note + triggers reject."""

    def _stub_scorer(self, all_score: int = 3):
        """Return a stub scorer_fn that yields a ScoringResult with uniform sub-scores."""
        assessment = _make_assessment(all_score)

        def scorer(job, conn_arg, cfg, candidate_context):
            return ScoringResult(status="ok", data=assessment, provider="ollama",
                                 model="stub-model")

        return scorer

    def test_scam_jd_sets_legitimacy_note_and_reject(self, db_conn):
        """Acceptance criterion 3 (end-to-end): flagged JD → legitimacy_note non-NULL
        AND classification='reject', even though sub_scores are all 3 (would be
        'apply' without the flag)."""
        conn, _ = db_conn
        job = _insert_job(conn, "job-scam", _SCAM_JD)

        so.score_and_persist_job(job, conn, _BASE_CONFIG,
                                 scorer_fn=self._stub_scorer(all_score=3))

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("job-scam",),
        ).fetchone()
        assert row["legitimacy_note"] is not None, (
            "Expected legitimacy_note to be set for scam JD"
        )
        assert "suspicious_pattern" in row["legitimacy_note"]
        # Confirm the reject branch fired — this is the previously-silent dead logic.
        assert row["classification"] == "reject", (
            f"Expected classification='reject' but got {row['classification']!r}"
        )

    def test_clean_jd_leaves_legitimacy_note_null(self, db_conn):
        """Acceptance criterion 4: clean JD → legitimacy_note NULL, classification
        from sub_scores (all 3 → 'apply')."""
        conn, _ = db_conn
        job = _insert_job(conn, "job-clean", _CLEAN_JD)

        so.score_and_persist_job(job, conn, _BASE_CONFIG,
                                 scorer_fn=self._stub_scorer(all_score=3))

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("job-clean",),
        ).fetchone()
        assert row["legitimacy_note"] is None, (
            f"Expected legitimacy_note=NULL for clean JD but got {row['legitimacy_note']!r}"
        )
        assert row["classification"] == "apply"

    def test_scam_jd_reject_beats_apply_subscores(self, db_conn):
        """The legitimacy_note reject branch takes precedence over otherwise-'apply'
        sub_scores — confirms the previously-silent dead logic in derive_classification
        is now active."""
        conn, _ = db_conn
        # Sub-scores all 4s → would classify as 'apply' without legitimacy_note.
        # JD must be >= 200 chars to clear the I-13 content-density trigger.
        scam_jd = (
            "Be your own boss — unlimited income potential awaits motivated "
            "self-starters willing to invest in themselves.  No prior experience "
            "needed.  Work flexible hours.  Join thousands who have already "
            "achieved financial freedom through our proven system.  Apply today."
        )
        job = _insert_job(conn, "job-scam2", scam_jd)

        so.score_and_persist_job(job, conn, _BASE_CONFIG,
                                 scorer_fn=self._stub_scorer(all_score=4))

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("job-scam2",),
        ).fetchone()
        assert row["legitimacy_note"] is not None
        assert row["classification"] == "reject"

    def test_existing_legitimacy_note_not_overwritten_by_clean_scan(self, db_conn):
        """Admin-set legitimacy_note on a clean-text job is preserved — the scanner
        only writes when it detects a pattern; it never clears existing notes."""
        conn, _ = db_conn
        job = _insert_job(conn, "job-admin-note", _CLEAN_JD)
        # Simulate admin manually flagging the job.
        conn.execute(
            "UPDATE jobs SET legitimacy_note = ? WHERE dedup_key = ?",
            ("manual-flag: recruiter requested fee", "job-admin-note"),
        )
        conn.commit()

        so.score_and_persist_job(job, conn, _BASE_CONFIG,
                                 scorer_fn=self._stub_scorer(all_score=3))

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("job-admin-note",),
        ).fetchone()
        # The admin note must survive.
        assert row["legitimacy_note"] == "manual-flag: recruiter requested fee"
        # And the pre-existing note still routes to reject.
        assert row["classification"] == "reject"
