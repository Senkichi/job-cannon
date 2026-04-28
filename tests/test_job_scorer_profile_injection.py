"""Tests for candidate-context splicing in job_scorer (Phase 2a sub-fix 2/3).

Verifies _build_system_prompt() accepts a candidate_context arg and splices
it between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES per spec D-2.1, and
that score_job() threads the parameter through to call_model.
"""

from __future__ import annotations

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
