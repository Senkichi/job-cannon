"""Tests for job_finder.web.legitimacy_scanner — Phase 49.07.

Covers:
1. scan_legitimacy returns a non-None note for each scam-pattern JD.
2. scan_legitimacy returns None for a clean JD.
3. Edge cases: None / empty jd_full.
4. End-to-end: score_and_persist_job on a flagged JD writes legitimacy_note
   non-NULL AND sets classification='reject' (confirms the previously-silent
   `if legitimacy_note: reject` branch in derive_classification now fires).
5. End-to-end: clean JD leaves legitimacy_note NULL; classification is
   whatever derive_classification produces from the sub-scores.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.legitimacy_scanner import scan_legitimacy


# ---------------------------------------------------------------------------
# Unit tests — scan_legitimacy()
# ---------------------------------------------------------------------------


class TestScanLegitimacyPatterns:
    """Each scam/MLM pattern should trigger a non-None return."""

    @pytest.mark.parametrize(
        "jd_text, expected_tag",
        [
            ("We offer unlimited income potential for driven individuals!", "mlm_income"),
            ("Be your own boss and set your own hours.", "mlm_boss"),
            ("This is an exciting work from home opportunity.", "mlm_wfh"),
            ("Join our team of leaders and grow with us.", "mlm_leader"),
            ("Achieve financial freedom through our proven system.", "mlm_freedom"),
            ("Earn residual income while you sleep.", "mlm_residual"),
            ("Recruit your downline and watch your earnings grow.", "mlm_downline"),
            ("Your earnings depend on the effort you put in.", "mlm_earnings"),
            ("Take advantage of this crypto trading opportunity today.", "crypto"),
            ("earn $500/day from home — no experience needed!", "earn_per_day"),
        ],
    )
    def test_pattern_detected(self, jd_text: str, expected_tag: str) -> None:
        note = scan_legitimacy(jd_text)
        assert note is not None, f"Expected a note for tag={expected_tag!r}, got None"
        assert f"suspicious_pattern: {expected_tag}" == note

    def test_case_insensitive(self) -> None:
        """Literal patterns are matched case-insensitively."""
        note = scan_legitimacy("UNLIMITED INCOME POTENTIAL for everyone")
        assert note is not None
        assert "mlm_income" in note

    def test_returns_first_match(self) -> None:
        """When multiple patterns match, the first literal pattern wins."""
        jd = "unlimited income potential and financial freedom"
        note = scan_legitimacy(jd)
        assert note == "suspicious_pattern: mlm_income"


class TestScanLegitimacyClean:
    """Clean JDs (no scam signals) must return None."""

    def test_clean_jd_returns_none(self) -> None:
        clean_jd = (
            "We are looking for a senior software engineer to join our platform team. "
            "You will design and build distributed systems, collaborate with product "
            "managers and designers, and contribute to our engineering culture. "
            "Salary: $160k-$220k. Remote-friendly in the US. Benefits: equity, "
            "health, dental, vision."
        )
        assert scan_legitimacy(clean_jd) is None

    def test_none_input_returns_none(self) -> None:
        assert scan_legitimacy(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert scan_legitimacy("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert scan_legitimacy("   ") is None


# ---------------------------------------------------------------------------
# End-to-end tests via score_and_persist_job
# ---------------------------------------------------------------------------

# Shared fixtures for E2E tests


@pytest.fixture()
def db_conn():
    """Fully migrated DB in a temp file."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, path
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_job(conn: sqlite3.Connection, dedup_key: str, jd_full: str) -> None:
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, sources,
                          source_urls, source_id, first_seen, last_seen,
                          score, score_breakdown, user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            "Account Executive",
            "Acme Sales Co",
            "Remote",
            '["test"]',
            '["https://example.com/job"]',
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


def _make_ok_result(sub_scores: dict | None = None):
    """Build a ScoringResult(status='ok') with configurable sub_scores."""
    from job_finder.db import JobAssessment
    from job_finder.web.job_scorer import ScoringResult

    if sub_scores is None:
        sub_scores = {
            "title_fit": 3,
            "location_fit": 3,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 3,
            "skills_match": 3,
        }
    assessment = JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale={
            "strengths": ["good match"],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        },
        provider="ollama",
    )
    return ScoringResult(status="ok", data=assessment, provider="ollama")


_BASE_CONFIG = {
    "providers": {
        "scoring": {"model": "qwen2.5:14b", "provider": "ollama"}
    }
}

_SCAM_JD = (
    "This is an exciting work from home opportunity with unlimited income potential. "
    "Be your own boss and achieve financial freedom. Join our team of leaders today "
    "and start earning residual income through our proven network marketing system. "
    "No experience required. Flexible hours. Must be motivated and self-directed."
)

_CLEAN_JD = (
    "We are hiring a senior backend engineer to join our platform team. You will "
    "design scalable APIs, mentor junior engineers, and collaborate closely with "
    "product managers and designers. Salary range: $160k-$220k. Fully remote in "
    "the US. Strong Python and distributed systems experience required."
)


class TestEndToEndScamJD:
    """End-to-end: scam JD → legitimacy_note non-NULL + classification='reject'."""

    def test_flagged_jd_sets_legitimacy_note_and_reject(self, db_conn) -> None:
        """Confirms the previously-silent `if legitimacy_note: reject` branch fires."""
        from job_finder.web import scoring_orchestrator as so

        conn, _ = db_conn
        dedup_key = "scam-job-001"
        _insert_job(conn, dedup_key, _SCAM_JD)

        job = {"dedup_key": dedup_key, "jd_full": _SCAM_JD}

        so.score_and_persist_job(
            job,
            conn,
            _BASE_CONFIG,
            scorer_fn=lambda j, c, cfg, candidate_context: _make_ok_result(),
        )

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row is not None
        assert row["legitimacy_note"] is not None, (
            "Expected legitimacy_note to be set for scam JD"
        )
        assert row["classification"] == "reject", (
            f"Expected classification='reject', got {row['classification']!r}"
        )

    def test_legitimacy_note_overrides_good_sub_scores(self, db_conn) -> None:
        """Even all-3 sub_scores (normally 'apply') become 'reject' when the
        scanner flags the JD — confirms the branch priority is correct."""
        from job_finder.web import scoring_orchestrator as so

        conn, _ = db_conn
        dedup_key = "scam-job-002"
        _insert_job(conn, dedup_key, _SCAM_JD)

        job = {"dedup_key": dedup_key, "jd_full": _SCAM_JD}
        # All 3s would produce 'apply' without legitimacy_note
        so.score_and_persist_job(
            job,
            conn,
            _BASE_CONFIG,
            scorer_fn=lambda j, c, cfg, candidate_context: _make_ok_result(
                {k: 3 for k in ("title_fit", "location_fit", "comp_fit",
                                 "domain_match", "seniority_match", "skills_match")}
            ),
        )

        row = conn.execute(
            "SELECT classification, legitimacy_note FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row["legitimacy_note"] is not None
        assert row["classification"] == "reject"


class TestEndToEndCleanJD:
    """End-to-end: clean JD → legitimacy_note NULL; classification from sub-scores."""

    def test_clean_jd_leaves_legitimacy_note_null(self, db_conn) -> None:
        from job_finder.web import scoring_orchestrator as so

        conn, _ = db_conn
        dedup_key = "clean-job-001"
        _insert_job(conn, dedup_key, _CLEAN_JD)

        job = {"dedup_key": dedup_key, "jd_full": _CLEAN_JD}
        so.score_and_persist_job(
            job,
            conn,
            _BASE_CONFIG,
            scorer_fn=lambda j, c, cfg, candidate_context: _make_ok_result(),
        )

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row is not None
        assert row["legitimacy_note"] is None, (
            f"Expected legitimacy_note=NULL for clean JD, got {row['legitimacy_note']!r}"
        )
        # All 3s → 'apply'
        assert row["classification"] == "apply"

    def test_clean_jd_classification_from_sub_scores(self, db_conn) -> None:
        """When no scam pattern, classification reflects sub-score rules."""
        from job_finder.web import scoring_orchestrator as so

        conn, _ = db_conn
        dedup_key = "clean-job-002"
        _insert_job(conn, dedup_key, _CLEAN_JD)

        job = {"dedup_key": dedup_key, "jd_full": _CLEAN_JD}
        # All 2s → 'consider'
        so.score_and_persist_job(
            job,
            conn,
            _BASE_CONFIG,
            scorer_fn=lambda j, c, cfg, candidate_context: _make_ok_result(
                {k: 2 for k in ("title_fit", "location_fit", "comp_fit",
                                 "domain_match", "seniority_match", "skills_match")}
            ),
        )

        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row["legitimacy_note"] is None
        assert row["classification"] == "consider"
