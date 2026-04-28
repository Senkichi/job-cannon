"""Tests for candidate-context splicing in job_scorer (Phase 2a sub-fixes 2/3 and 3/3).

Sub-fix 2/3: _build_system_prompt() accepts a candidate_context arg and splices
it between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES per spec D-2.1; score_job()
threads the parameter through to call_model.

Sub-fix 3/3 (integration): the orchestrator's score_and_persist_job accepts and
forwards candidate_context, and end-to-end the profile loaded from disk reaches
the scorer's system prompt.
"""

from __future__ import annotations

import json
import sqlite3

from job_finder.web.job_scorer import _build_system_prompt, score_job
from job_finder.web.model_provider import ModelResult


def test_build_system_prompt_includes_candidate_context_when_provided():
    ctx = "## Candidate context\n\n### Targeting\n- Target titles: Foo Analyst"
    prompt = _build_system_prompt(candidate_context=ctx)
    assert "Foo Analyst" in prompt
    # Splice point: between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES
    fr_idx = prompt.find("STRICT FIELD NAMES")  # first line of FIELD_REINFORCEMENT
    fs_idx = prompt.find("Fewshot calibration examples")
    ctx_idx = prompt.find("## Candidate context")
    assert fr_idx >= 0, "FIELD_REINFORCEMENT must appear in spliced prompt"
    assert fs_idx >= 0, "FEWSHOT_EXAMPLES must appear in spliced prompt"
    assert ctx_idx >= 0, "Candidate context must appear in spliced prompt"
    assert fr_idx < ctx_idx < fs_idx, (
        "Candidate context must be spliced between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES"
    )


def test_build_system_prompt_omits_section_when_no_context():
    prompt = _build_system_prompt(candidate_context=None)
    assert "## Candidate context" not in prompt
    # Sanity: existing prompt content still present
    assert "STRICT FIELD NAMES" in prompt
    assert "Fewshot calibration examples" in prompt


def test_score_job_threads_candidate_context_into_call_model(monkeypatch):
    """Verify that score_job passes candidate_context through to call_model."""
    captured: dict = {}

    def fake_call_model(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return ModelResult(
            data={
                "title_fit": 3,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            cost_usd=0.0,
            input_tokens=100,
            output_tokens=50,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )

    monkeypatch.setattr("job_finder.web.job_scorer.call_model", fake_call_model)

    conn = sqlite3.connect(":memory:")
    job = {
        "dedup_key": "x|y",
        "title": "T",
        "company": "C",
        "location": "Remote",
        "jd_full": "Long enough JD " * 50,
    }
    ctx = "## Candidate context\n- Target titles: Specific Role"
    result = score_job(job, conn, {}, candidate_context=ctx)
    assert result.status == "ok"
    assert "Specific Role" in captured["system"]


def test_orchestrator_passes_candidate_context_through(monkeypatch, tmp_path):
    """End-to-end: profile loaded from disk -> context built -> splice into system prompt."""
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "title": "Lead Analyst",
                        "company": "Foo",
                        "start_date": "2020",
                        "end_date": None,
                        "achievements": [],
                    }
                ],
                "skills": ["A/B testing", "BigQuery"],
                "education": [],
                "resume_preferences": {"summary_style": "concise", "emphasis": []},
            }
        )
    )

    config = {
        "profile_path": str(profile_path),
        "profile": {
            "target_titles": ["Lead Analyst", "Staff DS"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": ["Healthcare"],
            "exclusions": {"companies": [], "title_keywords": []},
        },
    }

    captured: dict = {}

    def fake_call_model(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return ModelResult(
            data={
                "title_fit": 3,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            cost_usd=0.0,
            input_tokens=100,
            output_tokens=50,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )

    monkeypatch.setattr("job_finder.web.job_scorer.call_model", fake_call_model)

    from job_finder.web.scoring_orchestrator import (
        build_candidate_context,
        load_scoring_profile,
        score_and_persist_job,
    )

    profile = load_scoring_profile(config)
    ctx = build_candidate_context(config, profile)

    # Minimal in-memory jobs schema that satisfies persist_job_assessment's
    # UPDATE-by-dedup-key contract. The UPDATE is a no-op when the row is
    # absent, so we only need an empty table for this test (no INSERT
    # required) -- but persist_job_assessment writes several columns that
    # must exist on the table even if the row is missing. We seed one row
    # so the persist path doesn't silently no-op the verification.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT, company TEXT, location TEXT,
            jd_full TEXT,
            classification TEXT,
            sub_scores_json TEXT,
            fit_analysis TEXT,
            scoring_provider TEXT,
            scoring_model TEXT,
            legitimacy_note TEXT,
            enrichment_tier TEXT,
            haiku_score REAL,
            sonnet_score REAL,
            haiku_summary TEXT,
            scored_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, jd_full) VALUES (?, ?, ?, ?, ?)",
        ("k|t", "Test", "Co", "Remote", "Long " * 100),
    )
    conn.commit()

    job = {
        "dedup_key": "k|t",
        "title": "Test",
        "company": "Co",
        "location": "Remote",
        "jd_full": "Long " * 100,
    }
    score_and_persist_job(job, conn, config, candidate_context=ctx)

    assert "Lead Analyst" in captured["system"]
    assert "BigQuery" in captured["system"]
    assert "150,000" in captured["system"]
