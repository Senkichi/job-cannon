"""Tests for resume_tailor module — guardrail tests against fabrication."""

import copy
import sqlite3
import subprocess
from unittest.mock import MagicMock

import pytest

from job_finder.web.resume_tailor import (
    NEVER_FABRICATE_INSTRUCTION,
    build_profile_facts,
    build_system_prompt,
    load_style_guide,
    tailor_resume,
)


def test_tailor_resume_dispatches_quick_tier_with_facts(app, tmp_path):
    """Assert tailor_resume dispatches exactly one call_model with tier="quick"
    and passes the anti-fabrication system prompt + profile facts + JD."""
    # Setup: mock call_model to return a fixed tailored-resume dict
    import shutil
    from pathlib import Path

    from job_finder.web.model_provider import ModelResult

    # Copy experience_profile.example.json to the test user data directory
    example_profile = Path(__file__).resolve().parents[1] / "experience_profile.example.json"
    user_data_root = tmp_path / "_userdata"
    shutil.copyfile(example_profile, user_data_root / "experience_profile.json")

    mock_result = ModelResult(
        data={
            "summary": "Test summary",
            "skills": ["Python", "SQL"],
            "sections": [
                {
                    "company": "TechCorp Solutions",
                    "title": "Senior Data Scientist",
                    "dates": "Mar 2022 - present",
                    "bullets": ["Built ML pipeline"],
                }
            ],
            "jd_keywords": ["machine learning", "data"],
        },
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="test-model",
        provider="test-provider",
        schema_valid=True,
    )

    with pytest.MonkeyPatch().context() as m:
        # Patch call_model at its source module
        m.setattr("job_finder.web.model_provider.call_model", MagicMock(return_value=mock_result))

        # Load a real profile fixture
        from job_finder.web.scoring_orchestrator import load_scoring_profile

        profile = load_scoring_profile(app.config)

        # Fixed job with jd_full
        job = {
            "dedup_key": "test-job-1",
            "title": "Data Scientist",
            "company": "TestCompany",
            "location": "Remote",
            "jd_full": "We are looking for a Data Scientist with Python and SQL experience.",
        }

        # Get a DB connection
        conn = sqlite3.connect(":memory:")

        # Call tailor_resume
        result = tailor_resume(job, profile, app.config, conn)

        # Assert call_model was called exactly once
        from job_finder.web import model_provider

        assert model_provider.call_model.call_count == 1

        # Assert tier kwarg == "quick"
        call_kwargs = model_provider.call_model.call_args.kwargs
        assert call_kwargs["tier"] == "quick"

        # Assert system kwarg contains source-fidelity/never-fabricate text
        system = call_kwargs["system"]
        assert "SOURCE FIDELITY" in system.upper()
        assert NEVER_FABRICATE_INSTRUCTION in system

        # Assert user message contains a distinctive profile fact (company name)
        user_content = call_kwargs["messages"][0]["content"]
        assert "TechCorp Solutions" in user_content

        # Assert user message contains a distinctive JD token
        assert "Python and SQL" in user_content


def test_system_prompt_forbids_fabrication_statically():
    """Assert build_system_prompt returns a prompt that forbids fabrication."""
    style_guide = load_style_guide()
    prompt = build_system_prompt(style_guide).lower()

    # Must contain both "fabricat" and "source fidelity"
    assert "fabricat" in prompt
    assert "source fidelity" in prompt


def test_style_guide_is_tracked_and_matches_backup():
    """Assert the style guide file is genuinely git-tracked and matches expected content."""
    import json
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    style_guide_path = (
        repo_root / "job_finder" / "web" / "scoring_prompts" / "resume_style_guide.json"
    )

    # (a) Assert the file is genuinely git-tracked
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(style_guide_path)],
        cwd=repo_root,
        capture_output=True,
    )
    assert result.returncode == 0, f"Style guide is not git-tracked: {result.stderr.decode()}"

    # (b) Assert the parsed JSON has the expected structure
    with open(style_guide_path, encoding="utf-8") as f:
        guide = json.load(f)

    # Must have the required keys
    assert "confidentiality_rules" in guide
    assert "jd_mirroring_rules" in guide
    assert "anti_patterns" in guide
    assert isinstance(guide["anti_patterns"], list)

    # Must contain the anti-fabrication text (in anti_patterns)
    assert any("fabricat" in pattern.lower() for pattern in guide["anti_patterns"])


def test_tailor_resume_does_not_mutate_inputs(app, tmp_path):
    """Assert tailor_resume does not mutate the job or profile arguments."""
    import shutil
    from pathlib import Path

    from job_finder.web.model_provider import ModelResult

    # Copy experience_profile.example.json to the test user data directory
    example_profile = Path(__file__).resolve().parents[1] / "experience_profile.example.json"
    user_data_root = tmp_path / "_userdata"
    shutil.copyfile(example_profile, user_data_root / "experience_profile.json")

    mock_result = ModelResult(
        data={"summary": "Test summary", "skills": ["Python"], "sections": [], "jd_keywords": []},
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="test-model",
        provider="test-provider",
        schema_valid=True,
    )

    with pytest.MonkeyPatch().context() as m:
        # Patch call_model at its source module
        m.setattr("job_finder.web.model_provider.call_model", MagicMock(return_value=mock_result))

        from job_finder.web.scoring_orchestrator import load_scoring_profile

        profile = load_scoring_profile(app.config)

        job = {
            "dedup_key": "test-job-2",
            "title": "Data Scientist",
            "company": "TestCompany",
            "location": "Remote",
            "jd_full": "Test JD",
        }

        # Deep-copy before call
        job_copy = copy.deepcopy(job)
        profile_copy = copy.deepcopy(profile)

        conn = sqlite3.connect(":memory:")

        # Call tailor_resume
        tailor_resume(job, profile, app.config, conn)

        # Assert originals are unchanged
        assert job == job_copy
        assert profile == profile_copy


def test_build_profile_facts_does_not_mutate_profile():
    """Assert build_profile_facts returns a new string and does not mutate profile."""
    profile = {
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "TechCorp Solutions",
                "start_date": "Mar 2022",
                "end_date": None,
                "achievements": ["Built ML pipeline"],
                "skills": ["Python"],
            }
        ],
        "skills": ["Python", "SQL"],
        "education": [],
    }

    profile_copy = copy.deepcopy(profile)
    facts = build_profile_facts(profile)

    # Assert profile is unchanged
    assert profile == profile_copy

    # Assert facts is a string
    assert isinstance(facts, str)

    # Assert facts contains expected content
    assert "TechCorp Solutions" in facts
    assert "Senior Data Scientist" in facts
    assert "Python" in facts
