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

    def mock_enrich(job_row, serpapi_key=None, conn=None, config=None):
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
            conn, serpapi_key=None, config={}
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

    def mock_enrich(job_row, serpapi_key=None, conn=None, config=None):
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
            conn, serpapi_key=None, config={}
        )

    # Multiple passes should complete and all 5 jobs should have been advanced.
    # Actual convergence: 5 jobs x 4 tier advancements each = 20 total before exhausted.
    # The test fixture advances through free->ddg->haiku->serpapi (4 tiers); pass 5 finds 0.
    # Note: >= 25 was wrong because the sonnet/exhausted tiers are terminal, not transitional.
    assert total_enriched >= 20, (
        f"Expected >=20 enrichments (5 jobs x 4 active tiers), got {total_enriched}"
    )
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
    assert "NULL" in captured.out, "NULL tier must appear in tier breakdown output"
    assert "$" in captured.out, "Cost estimate must be printed"
    assert "Eligible jobs" in captured.out, "Eligible jobs header must appear"

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
            conn, serpapi_key=None, config={}
        )

    # enrich_job should never be called
    mock_enrich.assert_not_called()
    assert total_enriched == 0
    assert len(tier_advanced_keys) == 0

# ---------------------------------------------------------------------------
# test_sonnet_queue
# ---------------------------------------------------------------------------

def test_scoring_backfill_classifies_unscored_jobs(migrated_db):
    """run_scoring_backfill scores jobs with jd_full but no v3 classification."""
    from job_finder.db import JobAssessment
    from job_finder.web.job_scorer import ScoringResult as JSResult

    path, conn = migrated_db

    insert_job(conn, "job1", jd_full="Full JD for job 1", sonnet_score=None)
    insert_job(conn, "job2", jd_full="Full JD for job 2", sonnet_score=None)
    insert_job(conn, "job3", jd_full="Full JD for job 3", sonnet_score=None)

    assessment = JobAssessment(
        sub_scores={"title_fit": 4, "location_fit": 3, "comp_fit": 4,
                    "domain_match": 5, "seniority_match": 4, "skills_match": 3},
        classification="",
        rationale={"strengths": [], "gaps": [],
                   "talking_points": [], "resume_priority_skills": []},
        provider="ollama",
    )
    mock_score_job = MagicMock(return_value=JSResult(
        status="ok", data=assessment, provider="ollama",
    ))

    with patch.object(be_module, "score_job", mock_score_job):
        count = be_module.run_scoring_backfill(conn, config={})

    assert count == 3
    assert mock_score_job.call_count == 3

    rows = conn.execute(
        "SELECT dedup_key, classification FROM jobs "
        "WHERE dedup_key IN ('job1','job2','job3')"
    ).fetchall()
    for row in rows:
        assert dict(row)["classification"] in {"apply", "consider", "skip", "reject"}


def test_scoring_backfill_skips_already_classified(migrated_db):
    """run_scoring_backfill skips jobs that already have classification."""
    from job_finder.db import JobAssessment
    from job_finder.web.job_scorer import ScoringResult as JSResult

    path, conn = migrated_db

    insert_job(conn, "already_scored", jd_full="Full JD", sonnet_score=None)
    conn.execute(
        "UPDATE jobs SET classification = 'apply' WHERE dedup_key = 'already_scored'"
    )
    conn.commit()
    insert_job(conn, "needs_scoring", jd_full="Full JD 2", sonnet_score=None)

    assessment = JobAssessment(
        sub_scores={"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                    "domain_match": 3, "seniority_match": 3, "skills_match": 3},
        classification="",
        rationale={"strengths": [], "gaps": [],
                   "talking_points": [], "resume_priority_skills": []},
        provider="ollama",
    )
    mock_score_job = MagicMock(return_value=JSResult(
        status="ok", data=assessment, provider="ollama",
    ))

    with patch.object(be_module, "score_job", mock_score_job):
        count = be_module.run_scoring_backfill(conn, config={})

    assert count == 1
    assert mock_score_job.call_count == 1
    row = conn.execute(
        "SELECT classification FROM jobs WHERE dedup_key = 'already_scored'"
    ).fetchone()
    assert dict(row)["classification"] == "apply"  # unchanged

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

    def mock_enrich(job_row, serpapi_key=None, conn=None, config=None):
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
        be_module.run_passes_to_convergence(conn, serpapi_key=None, config={})

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

    def mock_enrich(job_row, serpapi_key=None, conn=None, config=None):
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
            conn, serpapi_key=None, config={}
        )

    # Only "advances" job had tier advance (from None to "free")
    assert "advances" in tier_advanced_keys
    # enriched_count reflects how many got non-empty results
    assert enriched_count >= 1

# ---------------------------------------------------------------------------
# test_sonnet_backfill_writes_fit_analysis
# ---------------------------------------------------------------------------

def test_scoring_backfill_writes_fit_analysis(migrated_db):
    """run_scoring_backfill persists rationale JSON to fit_analysis column."""
    from job_finder.db import JobAssessment
    from job_finder.web.job_scorer import ScoringResult as JSResult

    path, conn = migrated_db
    insert_job(conn, "job_with_jd", jd_full="Full job description here", sonnet_score=None)

    rationale = {
        "strengths": ["Python", "ML"],
        "gaps": ["Leadership"],
        "talking_points": ["Experience with large datasets"],
        "resume_priority_skills": ["Python", "SQL"],
    }
    assessment = JobAssessment(
        sub_scores={"title_fit": 4, "location_fit": 4, "comp_fit": 4,
                    "domain_match": 4, "seniority_match": 4, "skills_match": 4},
        classification="",
        rationale=rationale,
        provider="ollama",
    )
    mock_score_job = MagicMock(return_value=JSResult(
        status="ok", data=assessment, provider="ollama",
    ))

    with patch.object(be_module, "score_job", mock_score_job):
        be_module.run_scoring_backfill(conn, config={})

    row = conn.execute(
        "SELECT classification, fit_analysis FROM jobs WHERE dedup_key = 'job_with_jd'"
    ).fetchone()
    row_dict = dict(row)
    assert row_dict["classification"] == "apply"  # all sub-scores >= 3
    parsed = json.loads(row_dict["fit_analysis"])
    assert parsed["strengths"] == ["Python", "ML"]


# ---------------------------------------------------------------------------
# test_agentic_tier_excluded_from_eligible_tiers_query
# ---------------------------------------------------------------------------


def test_agentic_and_agentic_exhausted_excluded_from_backfill(migrated_db):
    """_ELIGIBLE_TIERS_QUERY excludes 'agentic' and 'agentic_exhausted' jobs."""
    path, conn = migrated_db

    # Insert jobs at each tier that SHOULD be excluded
    insert_job(conn, "job_agentic", enrichment_tier="agentic")
    insert_job(conn, "job_agentic_exhausted", enrichment_tier="agentic_exhausted")
    insert_job(conn, "job_exhausted", enrichment_tier="exhausted")
    insert_job(conn, "job_serpapi", enrichment_tier="serpapi")
    insert_job(conn, "job_sonnet", enrichment_tier="sonnet")
    # This one should be ELIGIBLE
    insert_job(conn, "job_null_tier", enrichment_tier=None)

    from job_finder.web.backfill_enrichment import _ELIGIBLE_TIERS_QUERY

    rows = conn.execute(
        f"SELECT dedup_key FROM jobs WHERE {_ELIGIBLE_TIERS_QUERY}"
    ).fetchall()
    eligible_keys = {dict(r)["dedup_key"] for r in rows}

    # agentic and agentic_exhausted must NOT appear
    assert "job_agentic" not in eligible_keys, "'agentic' tier must be excluded from backfill"
    assert "job_agentic_exhausted" not in eligible_keys, "'agentic_exhausted' tier must be excluded"
    # exhausted, serpapi, sonnet must also be excluded
    assert "job_exhausted" not in eligible_keys
    assert "job_serpapi" not in eligible_keys
    assert "job_sonnet" not in eligible_keys
    # NULL tier (unenriched) must be eligible
    assert "job_null_tier" in eligible_keys


# ---------------------------------------------------------------------------
# Offline-config plumbing tests
# ---------------------------------------------------------------------------

def test_run_enrichment_pass_wraps_config_through_offline_providers(migrated_db):
    """run_enrichment_pass must pass the cascade-enabled config to enrich_job.

    Without the _offline_config wrapper, enrich_job would call enrichment_tiers
    with the raw user config and the Haiku/Sonnet tiers would stay on the
    direct CLI path even after the tier migrations — defeating backfill's
    whole reason for opting into Ollama.
    """
    path, conn = migrated_db
    insert_job(conn, "job_offline_probe")

    captured_configs: list[dict] = []

    def _capture(job_row, serpapi_key=None, conn=None, config=None):
        captured_configs.append(config)
        # Advance to exhausted so the pass terminates cleanly
        if conn is not None:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = 'exhausted' WHERE dedup_key = ?",
                (job_row["dedup_key"],),
            )
            conn.commit()
        return {"jd_full": "done"}

    with patch.object(be_module, "enrich_job", side_effect=_capture):
        be_module.run_enrichment_pass(conn, serpapi_key=None, config={})

    assert captured_configs, "enrich_job was never called"
    injected = captured_configs[0]
    providers = injected.get("providers", {})
    assert "scoring" in providers
    assert providers["scoring"]["provider"] == "ollama"


def test_offline_providers_use_fallback_chain_not_fallback():
    """_OFFLINE_PROVIDERS entries must use fallback_chain (list) — not the
    singular fallback (string), which triggers call_model's backward-compat
    path and raises generic RuntimeError instead of
    ProviderCascadeExhaustedError."""
    from job_finder.web.backfill_enrichment import _OFFLINE_PROVIDERS

    for tier, cfg in _OFFLINE_PROVIDERS.items():
        assert "fallback_chain" in cfg, f"{tier!r} should use fallback_chain"
        assert isinstance(cfg["fallback_chain"], list)
        assert cfg["fallback_chain"], f"{tier!r} fallback_chain must be non-empty"
        assert "fallback" not in cfg, (
            f"{tier!r} still uses the singular fallback key; "
            "call_model cascade path won't activate"
        )
