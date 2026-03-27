"""Unit tests for enrichment backfill script.

Tests convergence loop, cost confirmation gate, Sonnet queue, and borderline
Haiku re-score behaviors. All AI calls and external dependencies are mocked.
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import job_finder.web.backfill_enrichment as be_module
from job_finder.web.scoring_types import ScoringResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def insert_job(conn: sqlite3.Connection, dedup_key: str, **kwargs) -> None:
    """Insert a minimal job row into the test DB."""
    defaults = {
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "sources": '["linkedin"]',
        "source_urls": '["https://linkedin.com/jobs/1"]',
        "source_id": "1",
        "salary_min": None,
        "salary_max": None,
        "description": "Build ML models.",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-03-01T00:00:00",
        "score": 0.0,
        "score_breakdown": "{}",
        "user_interest": "unreviewed",
        "haiku_score": None,
        "haiku_summary": None,
        "sonnet_score": None,
        "fit_analysis": None,
        "jd_full": None,
        "enrichment_tier": None,
        "pipeline_status": "discovered",
    }
    defaults.update(kwargs)
    conn.execute(
        """INSERT OR REPLACE INTO jobs
        (dedup_key, title, company, location, sources, source_urls, source_id,
         salary_min, salary_max, description, first_seen, last_seen, score,
         score_breakdown, user_interest, haiku_score, haiku_summary,
         sonnet_score, fit_analysis, jd_full, enrichment_tier,
         pipeline_status)
        VALUES
        (:dedup_key, :title, :company, :location, :sources, :source_urls, :source_id,
         :salary_min, :salary_max, :description, :first_seen, :last_seen, :score,
         :score_breakdown, :user_interest, :haiku_score, :haiku_summary,
         :sonnet_score, :fit_analysis, :jd_full, :enrichment_tier,
         :pipeline_status)""",
        {"dedup_key": dedup_key, **defaults},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# test_convergence
# ---------------------------------------------------------------------------

def test_convergence(migrated_db):
    """One pass converges when enrich_job returns exhausted tier on first call."""
    path, conn = migrated_db

    # Insert 3 jobs at NULL enrichment tier
    insert_job(conn, "job1")
    insert_job(conn, "job2")
    insert_job(conn, "job3")

    def mock_enrich(job_row, serpapi_key=None, anthropic_client=None, conn=None, config=None):
        # Advance to exhausted tier immediately
        if conn is not None:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = 'exhausted' WHERE dedup_key = ?",
                (job_row["dedup_key"],),
            )
            conn.commit()
        return {"jd_full": "Job description text"}

    with patch.object(be_module, "enrich_job", side_effect=mock_enrich), \
         patch.object(be_module, "estimate_and_confirm", return_value=True):
        total_enriched, tier_advanced_keys = be_module.run_passes_to_convergence(
            conn, serpapi_key=None, config={}, client=MagicMock()
        )

    # First pass enriches 3, second pass returns 0 (all exhausted)
    assert total_enriched == 3
    # All 3 dedup_keys tracked as tier-advanced
    assert len(tier_advanced_keys) == 3
    assert "job1" in tier_advanced_keys
    assert "job2" in tier_advanced_keys
    assert "job3" in tier_advanced_keys


# ---------------------------------------------------------------------------
# test_convergence_multiple_passes
# ---------------------------------------------------------------------------

def test_convergence_multiple_passes(migrated_db):
    """Multiple passes are needed when enrich_job advances tier one step at a time."""
    path, conn = migrated_db

    # Insert 5 jobs at NULL enrichment tier
    for i in range(5):
        insert_job(conn, f"job{i}")

    # Track call count per job to advance tier step by step
    # free -> ddg -> haiku -> serpapi -> sonnet -> exhausted (5 steps)
    tier_progression = ["free", "ddg", "haiku", "serpapi", "sonnet", "exhausted"]

    def mock_enrich(job_row, serpapi_key=None, anthropic_client=None, conn=None, config=None):
        key = job_row["dedup_key"]
        current = job_row.get("enrichment_tier")
        # Advance one tier
        if current is None:
            next_tier = "free"
        elif current == "exhausted":
            return {}
        else:
            idx = tier_progression.index(current)
            if idx + 1 >= len(tier_progression):
                next_tier = "exhausted"
            else:
                next_tier = tier_progression[idx + 1]

        if conn is not None:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (next_tier, key),
            )
            conn.commit()
        return {"jd_full": "text"} if next_tier != "exhausted" else {}

    with patch.object(be_module, "enrich_job", side_effect=mock_enrich), \
         patch.object(be_module, "estimate_and_confirm", return_value=True):
        total_enriched, tier_advanced_keys = be_module.run_passes_to_convergence(
            conn, serpapi_key=None, config={}, client=MagicMock()
        )

    # Multiple passes should complete and all 5 jobs should have been advanced
    assert total_enriched > 5  # multiple passes × 5 jobs
    assert len(tier_advanced_keys) == 5


# ---------------------------------------------------------------------------
# test_cost_estimate
# ---------------------------------------------------------------------------

def test_cost_estimate_yes(migrated_db, monkeypatch):
    """estimate_and_confirm prints tier breakdown and returns True on 'y'."""
    path, conn = migrated_db

    # Insert jobs at different enrichment tiers
    insert_job(conn, "null_job1", enrichment_tier=None)
    insert_job(conn, "null_job2", enrichment_tier=None)
    insert_job(conn, "free_job", enrichment_tier="free")
    insert_job(conn, "ddg_job", enrichment_tier="ddg")
    insert_job(conn, "exhausted_job", enrichment_tier="exhausted")  # should be excluded

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    result = be_module.estimate_and_confirm(conn, config={})
    assert result is True


def test_cost_estimate_no(migrated_db, monkeypatch):
    """estimate_and_confirm returns False on 'n'."""
    path, conn = migrated_db
    insert_job(conn, "null_job", enrichment_tier=None)

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    result = be_module.estimate_and_confirm(conn, config={})
    assert result is False


def test_cost_estimate_default_no(migrated_db, monkeypatch):
    """estimate_and_confirm returns False on empty Enter (default N)."""
    path, conn = migrated_db
    insert_job(conn, "null_job", enrichment_tier=None)

    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    result = be_module.estimate_and_confirm(conn, config={})
    assert result is False


def test_cost_estimate_counts_tiers(migrated_db, monkeypatch, capsys):
    """estimate_and_confirm correctly counts jobs at each eligible tier."""
    path, conn = migrated_db

    insert_job(conn, "null1", enrichment_tier=None)
    insert_job(conn, "null2", enrichment_tier=None)
    insert_job(conn, "free1", enrichment_tier="free")
    insert_job(conn, "ddg1", enrichment_tier="ddg")
    insert_job(conn, "haiku1", enrichment_tier="haiku")
    insert_job(conn, "exhausted1", enrichment_tier="exhausted")  # excluded

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    be_module.estimate_and_confirm(conn, config={})
    captured = capsys.readouterr()

    # Should show tier counts and an estimated cost in the output
    assert "NULL" in captured.out or "null" in captured.out.lower()
    assert "$" in captured.out  # cost estimate printed


# ---------------------------------------------------------------------------
# test_no_ai_calls_without_confirmation
# ---------------------------------------------------------------------------

def test_no_ai_calls_without_confirmation(migrated_db):
    """run_passes_to_convergence aborts without calling enrich_job if user declines."""
    path, conn = migrated_db
    insert_job(conn, "job1")

    mock_enrich = MagicMock(return_value={})

    with patch.object(be_module, "enrich_job", mock_enrich), \
         patch.object(be_module, "estimate_and_confirm", return_value=False):
        total_enriched, tier_advanced_keys = be_module.run_passes_to_convergence(
            conn, serpapi_key=None, config={}, client=MagicMock()
        )

    # enrich_job should never be called
    mock_enrich.assert_not_called()
    assert total_enriched == 0
    assert len(tier_advanced_keys) == 0


# ---------------------------------------------------------------------------
# test_sonnet_queue
# ---------------------------------------------------------------------------

def test_sonnet_queue(migrated_db):
    """run_sonnet_backfill calls evaluate_job_sonnet for jobs with jd_full but no sonnet_score."""
    path, conn = migrated_db

    # 3 jobs with jd_full but no sonnet_score
    insert_job(conn, "job1", jd_full="Full JD for job 1", sonnet_score=None)
    insert_job(conn, "job2", jd_full="Full JD for job 2", sonnet_score=None)
    insert_job(conn, "job3", jd_full="Full JD for job 3", sonnet_score=None)

    mock_data = {
        "score": 82,
        "summary": "Strong match",
        "fit_analysis": {"strengths": [], "gaps": [], "talking_points": [], "resume_priority_skills": []},
    }
    mock_evaluate = MagicMock(return_value=ScoringResult(data=mock_data, status="success"))

    with patch.object(be_module, "evaluate_job_sonnet", mock_evaluate):
        count = be_module.run_sonnet_backfill(conn, config={}, client=MagicMock())

    assert count == 3
    assert mock_evaluate.call_count == 3

    # Verify sonnet_score written to DB
    rows = conn.execute(
        "SELECT dedup_key, sonnet_score FROM jobs WHERE dedup_key IN ('job1','job2','job3')"
    ).fetchall()
    for row in rows:
        assert dict(row)["sonnet_score"] == 82


# ---------------------------------------------------------------------------
# test_sonnet_queue_skips_scored
# ---------------------------------------------------------------------------

def test_sonnet_queue_skips_scored(migrated_db):
    """run_sonnet_backfill skips jobs that already have sonnet_score."""
    path, conn = migrated_db

    # One job with jd_full AND sonnet_score already set
    insert_job(conn, "already_scored", jd_full="Full JD", sonnet_score=75)
    # One job that needs scoring
    insert_job(conn, "needs_scoring", jd_full="Full JD 2", sonnet_score=None)

    mock_data = {
        "score": 80,
        "summary": "Good match",
        "fit_analysis": {"strengths": [], "gaps": [], "talking_points": [], "resume_priority_skills": []},
    }
    mock_evaluate = MagicMock(return_value=ScoringResult(data=mock_data, status="success"))

    with patch.object(be_module, "evaluate_job_sonnet", mock_evaluate):
        count = be_module.run_sonnet_backfill(conn, config={}, client=MagicMock())

    assert count == 1  # Only 1 job evaluated
    assert mock_evaluate.call_count == 1
    # Verify the already_scored job was NOT re-evaluated
    row = conn.execute(
        "SELECT sonnet_score FROM jobs WHERE dedup_key = 'already_scored'"
    ).fetchone()
    assert dict(row)["sonnet_score"] == 75  # unchanged


# ---------------------------------------------------------------------------
# test_borderline_rescore
# ---------------------------------------------------------------------------

def test_borderline_rescore(migrated_db):
    """run_borderline_rescore calls score_job_haiku for borderline jobs whose tier advanced."""
    path, conn = migrated_db

    # Insert borderline job (haiku_score 40-70) that had its tier advanced
    insert_job(conn, "borderline1", haiku_score=55, enrichment_tier="haiku")
    insert_job(conn, "borderline2", haiku_score=42, enrichment_tier="ddg")

    tier_advanced_keys = {"borderline1", "borderline2"}

    mock_data = {
        "score": 65,
        "summary": "Re-scored after enrichment",
        "title_fit": "partial",
        "location_fit": "remote",
        "salary_meets_floor": True,
    }
    mock_score = MagicMock(return_value=ScoringResult(data=mock_data, status="success"))

    with patch.object(be_module, "score_job_haiku", mock_score):
        count = be_module.run_borderline_rescore(
            conn, config={}, client=MagicMock(), tier_advanced_keys=tier_advanced_keys
        )

    assert count == 2
    assert mock_score.call_count == 2

    # Verify haiku_score written to DB
    rows = conn.execute(
        "SELECT dedup_key, haiku_score FROM jobs WHERE dedup_key IN ('borderline1', 'borderline2')"
    ).fetchall()
    for row in rows:
        assert dict(row)["haiku_score"] == 65


# ---------------------------------------------------------------------------
# test_borderline_skips_non_advanced
# ---------------------------------------------------------------------------

def test_borderline_skips_non_advanced(migrated_db):
    """run_borderline_rescore skips borderline jobs whose tier did NOT advance."""
    path, conn = migrated_db

    # Borderline job that was NOT in tier_advanced_keys
    insert_job(conn, "borderline_not_advanced", haiku_score=55)
    # Borderline job that WAS advanced
    insert_job(conn, "borderline_advanced", haiku_score=60)

    # Only the second job is in tier_advanced_keys
    tier_advanced_keys = {"borderline_advanced"}

    mock_score = MagicMock(return_value=ScoringResult(data={
        "score": 70,
        "summary": "Better after enrichment",
        "title_fit": "strong",
        "location_fit": "remote",
        "salary_meets_floor": True,
    }, status="success"))

    with patch.object(be_module, "score_job_haiku", mock_score):
        count = be_module.run_borderline_rescore(
            conn, config={}, client=MagicMock(), tier_advanced_keys=tier_advanced_keys
        )

    # Only 1 job re-scored (the one in tier_advanced_keys)
    assert count == 1
    assert mock_score.call_count == 1
    # non-advanced job's score should be unchanged
    row = conn.execute(
        "SELECT haiku_score FROM jobs WHERE dedup_key = 'borderline_not_advanced'"
    ).fetchone()
    assert dict(row)["haiku_score"] == 55


# ---------------------------------------------------------------------------
# test_ordering_by_score_desc
# ---------------------------------------------------------------------------

def test_ordering_by_score_desc(migrated_db):
    """Enrichment query uses ORDER BY COALESCE(haiku_score, 0) DESC so high-value jobs first."""
    path, conn = migrated_db

    # Insert jobs with different haiku scores at NULL enrichment tier
    insert_job(conn, "low_score", haiku_score=30, enrichment_tier=None)
    insert_job(conn, "high_score", haiku_score=85, enrichment_tier=None)
    insert_job(conn, "no_score", haiku_score=None, enrichment_tier=None)

    call_order = []

    def mock_enrich(job_row, serpapi_key=None, anthropic_client=None, conn=None, config=None):
        call_order.append(job_row["dedup_key"])
        # Advance to exhausted so convergence happens in 1 pass
        if conn is not None:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = 'exhausted' WHERE dedup_key = ?",
                (job_row["dedup_key"],),
            )
            conn.commit()
        return {}

    with patch.object(be_module, "enrich_job", side_effect=mock_enrich), \
         patch.object(be_module, "estimate_and_confirm", return_value=True):
        be_module.run_passes_to_convergence(conn, serpapi_key=None, config={}, client=MagicMock())

    # high_score (85) should come before low_score (30) and no_score (NULL=0)
    assert call_order[0] == "high_score"
    assert call_order[1] == "low_score"
    assert call_order[2] == "no_score"


# ---------------------------------------------------------------------------
# test_run_enrichment_pass_tracks_tier_advancement
# ---------------------------------------------------------------------------

def test_run_enrichment_pass_tracks_tier_advancement(migrated_db):
    """run_enrichment_pass returns set of dedup_keys whose enrichment_tier advanced."""
    path, conn = migrated_db

    insert_job(conn, "advances", enrichment_tier=None)
    insert_job(conn, "no_change", enrichment_tier=None)
    insert_job(conn, "exhausted_already", enrichment_tier="exhausted")

    def mock_enrich(job_row, serpapi_key=None, anthropic_client=None, conn=None, config=None):
        key = job_row["dedup_key"]
        if key == "advances":
            if conn is not None:
                conn.execute(
                    "UPDATE jobs SET enrichment_tier = 'free' WHERE dedup_key = ?", (key,)
                )
                conn.commit()
            return {"jd_full": "some text"}
        else:
            # no-change: enrich returns empty, tier stays None
            return {}

    with patch.object(be_module, "enrich_job", side_effect=mock_enrich):
        enriched_count, tier_advanced_keys = be_module.run_enrichment_pass(
            conn, serpapi_key=None, config={}, client=MagicMock()
        )

    # Only "advances" job had tier advance (from None to "free")
    assert "advances" in tier_advanced_keys
    # enriched_count reflects how many got non-empty results
    assert enriched_count >= 1


# ---------------------------------------------------------------------------
# test_sonnet_backfill_writes_fit_analysis
# ---------------------------------------------------------------------------

def test_sonnet_backfill_writes_fit_analysis(migrated_db):
    """run_sonnet_backfill persists fit_analysis JSON to DB."""
    path, conn = migrated_db

    insert_job(conn, "job_with_jd", jd_full="Full job description here", sonnet_score=None)

    fit = {
        "strengths": ["Python", "ML"],
        "gaps": ["Leadership"],
        "talking_points": ["Experience with large datasets"],
        "resume_priority_skills": ["Python", "SQL"],
    }
    mock_data = {"score": 78, "summary": "Good fit", "fit_analysis": fit}
    mock_evaluate = MagicMock(return_value=ScoringResult(data=mock_data, status="success"))

    with patch.object(be_module, "evaluate_job_sonnet", mock_evaluate):
        be_module.run_sonnet_backfill(conn, config={}, client=MagicMock())

    row = conn.execute(
        "SELECT sonnet_score, fit_analysis FROM jobs WHERE dedup_key = 'job_with_jd'"
    ).fetchone()
    row_dict = dict(row)
    assert row_dict["sonnet_score"] == 78
    parsed_fit = json.loads(row_dict["fit_analysis"])
    assert parsed_fit["strengths"] == ["Python", "ML"]
