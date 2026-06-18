"""Tests for job_finder.web.legitimacy_scanner (Phase 49.07).

Covers:
1. Unit: scan_legitimacy returns a non-None note for each scam/MLM pattern.
2. Unit: scan_legitimacy returns None for a clean JD.
3. Unit: scan_legitimacy returns None for empty/None input.
4. Unit: regex pattern fires for high-daily-earnings claim.
5. E2E: run_scoring on a flagged JD produces legitimacy_note non-NULL
        AND classification='reject' (previously-silent dead branch now fires).
6. E2E: run_scoring on a clean JD leaves legitimacy_note NULL;
        classification is whatever derive_classification produces from sub-scores.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from job_finder.web.legitimacy_scanner import scan_legitimacy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now().isoformat()

_SCORING_CFG = {
    "providers": {
        "scoring": {
            "model": "qwen2.5:14b",
            "provider": "ollama",
        },
    },
}


def _insert_job(conn: sqlite3.Connection, dedup_key: str, jd_full: str | None) -> None:
    """Insert a minimal jobs row."""
    conn.execute(
        """
        INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, first_seen, last_seen, score, score_breakdown,
             user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            "Marketing Manager",
            "Opportunity Inc",
            "Remote",
            '["test"]',
            '["https://example.com/job/1"]',
            "src-001",
            _NOW,
            _NOW,
            0.0,
            "{}",
            "unreviewed",
            jd_full,
        ),
    )
    conn.commit()


def _make_good_scorer():
    """Return a scorer stub that produces a strong sub-score vector ('apply').

    All-4s carries positive fit evidence (6 strong axes, mean 4.0), so
    derive_classification yields 'apply' — distinct from a flat-neutral all-3s
    vector, which now classifies 'low_signal' (issue #210).
    """
    from job_finder.db import JobAssessment
    from job_finder.web.job_scorer import ScoringResult

    def _scorer(job, conn, cfg, candidate_context):
        return ScoringResult(
            status="ok",
            data=JobAssessment(
                sub_scores={
                    "title_fit": 4,
                    "location_fit": 4,
                    "comp_fit": 4,
                    "domain_match": 4,
                    "seniority_match": 4,
                    "skills_match": 4,
                },
                classification="",
                rationale={
                    "strengths": ["good fit"],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                provider="ollama",
            ),
            provider="ollama",
        )

    return _scorer


# ---------------------------------------------------------------------------
# Unit tests — scan_legitimacy
# ---------------------------------------------------------------------------


class TestScanLegitimacyPhrases:
    """Each phrase pattern fires correctly."""

    @pytest.mark.parametrize(
        "jd_text,expected_prefix",
        [
            ("This role offers unlimited income potential for driven people.", "mlm_phrase"),
            ("Be your own boss and set your own schedule.", "mlm_phrase"),
            ("This is a work from home opportunity with flexible hours.", "mlm_phrase"),
            ("Join our team of leaders in the fastest-growing network.", "mlm_phrase"),
            ("Achieve financial freedom through our proven system.", "mlm_phrase"),
            ("Earn residual income that keeps paying month after month.", "mlm_phrase"),
            ("You will recruit your downline and coach them to success.", "mlm_phrase"),
            ("Your earnings depend on the effort you put in.", "mlm_phrase"),
            ("This is a crypto trading opportunity with daily returns.", "scam_phrase"),
        ],
    )
    def test_phrase_returns_note(self, jd_text: str, expected_prefix: str) -> None:
        note = scan_legitimacy(jd_text)
        assert note is not None, f"Expected a note for: {jd_text!r}"
        assert note.startswith(expected_prefix), (
            f"Expected note starting with {expected_prefix!r}, got {note!r}"
        )

    def test_regex_high_daily_earnings(self) -> None:
        """earn $500/day regex fires."""
        note = scan_legitimacy("You can earn $500/day working from home.")
        assert note is not None
        assert note.startswith("scam_phrase")

    def test_regex_high_daily_earnings_no_match_below_threshold(self) -> None:
        """earn $99/day does NOT fire (threshold is 3+ digits)."""
        note = scan_legitimacy("Earn $99/day as a side hustle.")
        assert note is None

    def test_case_insensitive_phrase(self) -> None:
        """Phrase match is case-insensitive."""
        note = scan_legitimacy("UNLIMITED INCOME POTENTIAL awaits you!")
        assert note is not None
        assert "unlimited income potential" in note

    def test_first_match_wins(self) -> None:
        """When multiple patterns match, only the first is returned."""
        jd = "unlimited income potential and financial freedom await"
        note = scan_legitimacy(jd)
        # First phrase in the table is 'unlimited income potential'
        assert note is not None
        assert "unlimited income potential" in note


class TestScanLegitimacyClean:
    """Clean JD → None."""

    def test_clean_jd_returns_none(self) -> None:
        jd = (
            "We are looking for a Senior Data Engineer to join our platform team. "
            "You will design and build scalable data pipelines, work closely with "
            "product and analytics teams, and mentor junior engineers. "
            "Requirements: 5+ years Python, strong SQL, cloud (AWS/GCP), "
            "experience with dbt or similar. Competitive salary + equity."
        )
        assert scan_legitimacy(jd) is None

    def test_empty_string_returns_none(self) -> None:
        assert scan_legitimacy("") is None

    def test_none_like_empty_returns_none(self) -> None:
        # The function signature accepts str; test with empty to cover the guard.
        assert scan_legitimacy("   ") is None


# ---------------------------------------------------------------------------
# End-to-end tests — run_scoring integration
# ---------------------------------------------------------------------------


class TestLegitimacyScannerE2E:
    """End-to-end: legitimacy_note written by run_scoring; derive_classification rejects."""

    def test_scam_jd_produces_legitimacy_note_and_reject(self, migrated_db) -> None:
        """E2E: Flagged JD → legitimacy_note non-NULL AND classification='reject'.

        This is the acceptance-criteria test confirming the previously-silent
        ``if legitimacy_note: reject`` branch in derive_classification now fires.
        The inner scorer returns all-3 sub-scores (which would produce 'apply'
        without the legitimacy_note override), so the only path to 'reject' is
        through the legitimacy branch.
        """
        db_path, setup_conn = migrated_db
        # JD must be ≥ 200 chars (I-13 trigger) and contain a scam phrase.
        scam_jd = (
            "This is a ground-floor work from home opportunity that offers "
            "unlimited income potential for the right motivated candidate. "
            "Achieve financial freedom by joining our fast-growing team of "
            "passionate go-getters. No prior experience is required — we "
            "provide full training and support from day one. Apply today!"
        )
        _insert_job(setup_conn, "scam-job-1", scam_jd)

        import job_finder.web.job_scorer as js
        import job_finder.web.scoring_runner as sr

        with (
            patch.object(js, "score_job", _make_good_scorer()),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(["scam-job-1"], _SCORING_CFG, db_path)

        assert summary["scored"] == 1

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("scam-job-1",),
        ).fetchone()
        conn.close()

        assert row["legitimacy_note"] is not None, (
            "legitimacy_note should be non-NULL after scanner fires"
        )
        assert row["classification"] == "reject", (
            f"Expected classification='reject', got {row['classification']!r}"
        )

    def test_clean_jd_leaves_legitimacy_note_null(self, migrated_db) -> None:
        """E2E: Clean JD → legitimacy_note is NULL; classification from sub-scores.

        The mock scorer returns a strong sub-score vector → derive_classification
        gives 'apply'.  Confirms the scanner does NOT fire on legitimate content.
        """
        db_path, setup_conn = migrated_db
        # JD must be ≥ 200 chars (I-13 trigger) and contain no scam patterns.
        clean_jd = (
            "We are seeking a Lead Data Engineer to build and own our core data "
            "infrastructure. You will design and implement scalable data pipelines, "
            "mentor junior engineers, and partner closely with product and analytics "
            "stakeholders. Requirements include 5+ years of Python and SQL, hands-on "
            "experience with Apache Spark or equivalent, and strong communication skills."
        )
        _insert_job(setup_conn, "clean-job-1", clean_jd)

        import job_finder.web.job_scorer as js
        import job_finder.web.scoring_runner as sr

        with (
            patch.object(js, "score_job", _make_good_scorer()),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(["clean-job-1"], _SCORING_CFG, db_path)

        assert summary["scored"] == 1

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT legitimacy_note, classification FROM jobs WHERE dedup_key = ?",
            ("clean-job-1",),
        ).fetchone()
        conn.close()

        assert row["legitimacy_note"] is None, (
            f"legitimacy_note should be NULL for clean JD, got {row['legitimacy_note']!r}"
        )
        assert row["classification"] == "apply", (
            f"Expected 'apply' from a strong sub-score vector, got {row['classification']!r}"
        )

    def test_preexisting_legitimacy_note_not_overwritten(self, migrated_db) -> None:
        """Manual legitimacy_note is preserved — scanner does not clobber it."""
        db_path, setup_conn = migrated_db
        # Job with a scam-pattern JD AND a pre-existing manual note.
        # JD must be ≥ 200 chars (I-13 trigger).
        scam_jd = (
            "Unlimited income potential awaits motivated self-starters in our "
            "growing network marketing division. Achieve financial freedom while "
            "working flexible hours from the comfort of your own home. Join a "
            "community of passionate leaders who are already living the dream. "
            "No experience required — we train you from the ground up. Act now!"
        )
        _insert_job(setup_conn, "manual-note-1", scam_jd)
        setup_conn.execute(
            "UPDATE jobs SET legitimacy_note = 'manual: reviewed by admin' WHERE dedup_key = ?",
            ("manual-note-1",),
        )
        setup_conn.commit()

        import job_finder.web.job_scorer as js
        import job_finder.web.scoring_runner as sr

        with (
            patch.object(js, "score_job", _make_good_scorer()),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            sr.run_scoring(["manual-note-1"], _SCORING_CFG, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT legitimacy_note FROM jobs WHERE dedup_key = ?",
            ("manual-note-1",),
        ).fetchone()
        conn.close()

        assert row["legitimacy_note"] == "manual: reviewed by admin", (
            "Pre-existing legitimacy_note must not be overwritten by the scanner"
        )
