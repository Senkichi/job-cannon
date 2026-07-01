"""CI gate test for application prepare feature (issue #599).

This test is constructed so a stubbed/faked implementation cannot pass:
- It seeds a real profile with distinctive sentinel values
- It seeds a job with a source_url so apply_url_for returns non-None
- It mocks only the resume-tailor seam, not the full assembler
- It spies on the call_model draft seam to verify it actually ran
- It asserts the row reflects real profile/LLM data, not hardcoded filler
- It asserts the dual-write (pipeline_status + pipeline_events) on approve
- It asserts no HTTP submission occurred
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest


def _clone_template(template_path: str) -> str:
    """Create a private on-disk copy of the migrated template; return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(template_path)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path


@pytest.fixture
def app_with_migrations(_migrated_template_db, tmp_path):
    """Flask app with fully-migrated DB and seeded profile/job."""
    from job_finder.web import create_app

    path = _clone_template(_migrated_template_db)

    # Seed profile with distinctive sentinel values (real schema with contact block)
    profile_path = tmp_path / "experience_profile.json"
    profile = {
        "contact": {
            "full_name": "PROFILE-NAME-SENTINEL",
            "email": "sentinel@example.com",
            "phone": "555-1234",
            "linkedin": "https://linkedin.com/in/sentinel",
            "github": "https://github.com/sentinel",
            "portfolio": "https://sentinel.dev",
            "location": "Remote",
        },
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "Test Company",
                "start_date": "2020-01-01",
                "end_date": None,
                "achievements": ["Built a thing"],
                "skills": ["Python", "ML"],
            }
        ],
        "skills": ["Python", "Machine Learning", "SQL"],
        "education": [],
    }
    profile_path.write_text(json.dumps(profile, indent=2))

    # Seed job with jd_full and source_url
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    jd_full = """We are looking for a Senior Data Scientist to join our team. You will be responsible for building machine learning models, analyzing large datasets, and collaborating with cross-functional teams to drive data-driven decisions. The ideal candidate has strong Python skills, experience with deep learning frameworks, and a passion for solving complex problems. This is a remote position with competitive compensation and benefits. You will work on cutting-edge projects that directly impact our business and customers. Our team values innovation, collaboration, and continuous learning. We offer a flexible work environment, professional development opportunities, and a comprehensive benefits package including health insurance, retirement plans, and generous vacation time. Join us in building the future of data science at our company."""
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description, jd_full,
             first_seen, last_seen, score_breakdown, user_interest, pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test|job|remote",
            "Test Job",
            "Test Company",
            "Remote",
            '["linkedin"]',
            '["https://example.com/apply"]',
            "12345",
            150000,
            200000,
            "Test description",
            jd_full,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
            "{}",
            "interested",
            "new",
        ),
    )
    conn.commit()
    conn.close()

    test_config = {
        "db": {"path": path},
        "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
        "profile": {
            "target_titles": ["Senior Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
        "application": {
            "draft_questions": [
                "Why do you want to work here?",
                "Summarize your relevant experience for this role.",
                "What is your greatest professional achievement?",
            ]
        },
        "providers": {
            "primary": "ollama",
            "fallback_chain": ["gemini", "claude_code_cli", "anthropic"],
            "overrides": {},
            "daily_limits": {},
            "throttle_delays": {},
        },
    }

    application = create_app(config=test_config)
    application.config["TESTING"] = True

    # Override profile path to use our seeded profile
    original_load_profile = None

    def mock_load_profile(profile_path="experience_profile.json"):
        return profile

    with patch("job_finder.web.application_prepare.load_profile", side_effect=mock_load_profile):
        yield application, path, profile_path


def test_prepare_application_end_to_end(app_with_migrations):
    """End-to-end test of prepare → approve flow with real data assertions."""
    app, db_path, profile_path = app_with_migrations
    client = app.test_client()

    dedup_key = "test|job|remote"

    # Mock the resume-tailor seam ONLY (return JSON string since we now serialize the dict)
    mock_resume_json = json.dumps({"summary": f"TAILORED-RESUME-SENTINEL-{dedup_key}"})
    with patch(
        "job_finder.web.application_prepare.tailor_resume",
        return_value=mock_resume_json,
    ):
        # Spy on call_model for draft answers
        from job_finder.web.model_provider import ModelResult

        call_model_spy = MagicMock(
            side_effect=lambda **kwargs: ModelResult(
                data=f"DRAFT-SENTINEL-{kwargs.get('job_id', 'unknown')}",
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                model="qwen2.5:14b",
                provider="ollama",
                schema_valid=True,
            )
        )

        # Block any HTTP submission
        with patch("requests.post", side_effect=AssertionError("HTTP POST should not occur")):
            with patch("httpx.post", side_effect=AssertionError("HTTP POST should not occur")):
                with patch(
                    "job_finder.web.application_prepare.call_model", side_effect=call_model_spy
                ):
                    # ACT: Prepare application
                    response = client.post(f"/jobs/{dedup_key}/prepare-application")
                    assert response.status_code == 200

                    # ASSERT: Row was created with real data
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM applications WHERE job_id = ?", (dedup_key,)
                    ).fetchone()
                    assert row is not None
                    assert row["status"] == "pending"
                    # resume_content is now JSON (serialized dict)
                    assert "TAILORED-RESUME-SENTINEL" in row["resume_content"]

                    # Parse JSON columns
                    form_mapping = json.loads(row["form_mapping_json"])
                    drafted_answers = json.loads(row["drafted_answers_json"])

                    # ASSERT: form_mapping contains profile sentinel (proves load_profile was called)
                    assert "PROFILE-NAME-SENTINEL" in json.dumps(form_mapping)
                    assert form_mapping["full_name"] == "PROFILE-NAME-SENTINEL"
                    assert form_mapping["email"] == "sentinel@example.com"
                    assert "apply_url" in form_mapping
                    assert form_mapping["apply_url"] == "https://example.com/apply"

                    # ASSERT: drafted_answers contain DRAFT-SENTINEL (proves call_model ran)
                    assert len(drafted_answers) > 0
                    assert any("DRAFT-SENTINEL" in ans for ans in drafted_answers.values())

                    # ASSERT: created_at is valid naive-UTC ISO string (no timezone offset / Z)
                    created_at = row["created_at"]
                    assert created_at.endswith("Z") is False  # No Z suffix
                    assert "+" not in created_at  # No + offset
                    assert "T" in created_at  # ISO format with T separator

                    # ASSERT: LLM draft seam actually ran
                    assert call_model_spy.call_count >= 1
                    # Verify it was called with tier="quick"
                    for call in call_model_spy.call_args_list:
                        assert call.kwargs.get("tier") == "quick"

                    # ASSERT: Review surface renders all three parts
                    response_text = response.data.decode("utf-8")
                    assert "TAILORED-RESUME-SENTINEL" in response_text
                    assert "PROFILE-NAME-SENTINEL" in response_text
                    assert "DRAFT-SENTINEL" in response_text

                    # ACT: Approve application
                    application_id = row["id"]
                    response = client.post(f"/jobs/applications/{application_id}/approve")
                    assert response.status_code == 200

                    # ASSERT: Dual-write (pipeline_status + pipeline_events)
                    job_row = conn.execute(
                        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (dedup_key,)
                    ).fetchone()
                    assert job_row is not None
                    assert job_row["pipeline_status"] == "applied"

                    event_count = conn.execute(
                        """SELECT COUNT(*) FROM pipeline_events
                           WHERE job_id = ? AND to_status = 'applied'""",
                        (dedup_key,),
                    ).fetchone()[0]
                    assert event_count >= 1

                    # ASSERT: Application row resolved
                    app_row = conn.execute(
                        "SELECT status, resolved_at FROM applications WHERE id = ?",
                        (application_id,),
                    ).fetchone()
                    assert app_row is not None
                    assert app_row["status"] == "approved"
                    assert app_row["resolved_at"] is not None

                    conn.close()

    # ASSERT: No HTTP submission occurred (patches would have raised if called)


def test_prepare_application_serializes_structured_resume(app_with_migrations):
    """Assembler serializes a structured resume dict from the seam into JSON storage.

    NOTE: this patches the tailor_resume SEAM (does not run the real resume_tailor);
    the genuine end-to-end seam is exercised by
    test_prepare_application_runs_real_tailor_seam below.
    """
    app, db_path, profile_path = app_with_migrations
    client = app.test_client()

    dedup_key = "test|job|remote"

    # Mock the seam to return JSON matching the real resume_tailor output schema
    mock_tailored_resume = {
        "summary": "Senior Data Scientist with 5+ years of experience in ML and data analysis.",
        "skills": ["Python", "Machine Learning", "SQL", "PyTorch", "scikit-learn"],
        "sections": [
            {
                "company": "Test Company",
                "title": "Senior Data Scientist",
                "dates": "2020-01-01 - present",
                "bullets": ["Built a thing", "Led a team"],
            }
        ],
        "jd_keywords": ["Python", "Machine Learning", "SQL", "remote"],
    }
    mock_resume_json = json.dumps(mock_tailored_resume)

    # Patch the seam (not the implementation) to return structured JSON
    with patch(
        "job_finder.web.application_prepare.tailor_resume",
        return_value=mock_resume_json,
    ):
        # Mock call_model for draft answers
        from job_finder.web.model_provider import ModelResult

        call_model_spy = MagicMock(
            side_effect=lambda **kwargs: ModelResult(
                data=f"DRAFT-ANSWER-{kwargs.get('job_id', 'unknown')}",
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                model="qwen2.5:14b",
                provider="ollama",
                schema_valid=True,
            )
        )

        # Block any HTTP submission
        with patch("requests.post", side_effect=AssertionError("HTTP POST should not occur")):
            with patch("httpx.post", side_effect=AssertionError("HTTP POST should not occur")):
                with patch(
                    "job_finder.web.application_prepare.call_model", side_effect=call_model_spy
                ):
                    # ACT: Prepare application
                    response = client.post(f"/jobs/{dedup_key}/prepare-application")
                    assert response.status_code == 200

                    # ASSERT: Row was created with structured resume data
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM applications WHERE job_id = ?", (dedup_key,)
                    ).fetchone()
                    assert row is not None
                    assert row["status"] == "pending"

                    # Parse resume_content as JSON (it's now a dict serialized to JSON)
                    resume_dict = json.loads(row["resume_content"])
                    assert resume_dict["summary"] == mock_tailored_resume["summary"]
                    assert resume_dict["skills"] == mock_tailored_resume["skills"]
                    assert len(resume_dict["sections"]) == 1
                    assert resume_dict["jd_keywords"] == mock_tailored_resume["jd_keywords"]

                    # ASSERT: form_mapping and drafted_answers are non-empty
                    form_mapping = json.loads(row["form_mapping_json"])
                    drafted_answers = json.loads(row["drafted_answers_json"])
                    assert len(form_mapping) > 0
                    assert len(drafted_answers) > 0
                    # Verify drafted answers contain our mock content
                    assert any("DRAFT-ANSWER" in ans for ans in drafted_answers.values())

                    # ASSERT: LLM was actually called for draft answers
                    assert call_model_spy.call_count >= 1
                    for call in call_model_spy.call_args_list:
                        assert call.kwargs.get("tier") == "quick"
                        assert call.kwargs.get("purpose") == "application_draft"

                    conn.close()

    # ASSERT: No HTTP submission occurred


def test_prepare_application_runs_real_tailor_seam(app_with_migrations):
    """Exercise the REAL resume_tailor through the UNPATCHED assembler seam.

    Patches ONLY call_model (both the resume_tailor internal import and the
    application_prepare draft import), NOT application_prepare.tailor_resume — so
    the real _tailor_resume adapter runs (positional call + json.dumps of the
    returned dict). Also feeds call_model an OLLAMA-SHAPED dict for the draft
    answers (the real provider returns a parsed dict, never a str) and asserts the
    persisted answer is clean prose, pinning the str(dict)-repr regression.
    """
    app, db_path, profile_path = app_with_migrations
    client = app.test_client()
    dedup_key = "test|job|remote"

    from job_finder.web.model_provider import ModelResult

    tailored_resume = {
        "summary": "REAL-SEAM-SUMMARY: Senior Data Scientist, 5+ years ML.",
        "skills": ["Python", "Machine Learning", "SQL"],
        "sections": [
            {
                "company": "Test Company",
                "title": "Senior Data Scientist",
                "dates": "2020-01-01 - present",
                "bullets": ["Built a thing"],
            }
        ],
        "jd_keywords": ["Python", "Machine Learning"],
    }

    def _resume_call_model(**kwargs):
        # resume_tailor's internal call_model -> return the structured resume DICT.
        return ModelResult(
            data=tailored_resume,
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )

    def _draft_call_model(**kwargs):
        # application_prepare's draft call_model -> ollama-shaped dict, NOT a str.
        return ModelResult(
            data={"answer": "I am genuinely excited about this role."},
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )

    with (
        patch("requests.post", side_effect=AssertionError("HTTP POST should not occur")),
        patch("httpx.post", side_effect=AssertionError("HTTP POST should not occur")),
        # resume_tailor imports call_model from model_provider INSIDE the function.
        patch("job_finder.web.model_provider.call_model", side_effect=_resume_call_model),
        # application_prepare imports call_model at module level for draft answers.
        patch("job_finder.web.application_prepare.call_model", side_effect=_draft_call_model),
    ):
        response = client.post(f"/jobs/{dedup_key}/prepare-application")
        assert response.status_code == 200

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM applications WHERE job_id = ?", (dedup_key,)).fetchone()
        assert row is not None, "the real seam should produce a stored package"

        # resume_content must be json.dumps of the DICT the real resume_tailor returned
        # (proves the _tailor_resume adapter ran: positional call + json.dumps).
        resume_dict = json.loads(row["resume_content"])
        assert resume_dict["summary"] == tailored_resume["summary"]
        assert resume_dict["skills"] == tailored_resume["skills"]

        # Drafted answers must be CLEAN PROSE, never a Python-repr of the dict.
        drafted_answers = json.loads(row["drafted_answers_json"])
        assert len(drafted_answers) > 0
        for ans in drafted_answers.values():
            assert ans == "I am genuinely excited about this role."
            assert "{" not in ans and "'answer'" not in ans, "answer must not be a str(dict) repr"
        conn.close()


def test_prepare_application_empty_profile_renders_error(app_with_migrations):
    """An empty profile (no positions/skills) makes the REAL resume_tailor raise
    ValueError; the route must render the error fragment at 200 and persist nothing."""
    app, db_path, profile_path = app_with_migrations
    client = app.test_client()
    dedup_key = "test|job|remote"

    from job_finder.web.profile_schema import EMPTY_PROFILE

    with (
        patch("requests.post", side_effect=AssertionError("HTTP POST should not occur")),
        # Override the fixture's profile mock with an EMPTY profile.
        patch(
            "job_finder.web.application_prepare.load_profile",
            return_value=dict(EMPTY_PROFILE),
        ),
    ):
        response = client.post(f"/jobs/{dedup_key}/prepare-application")
        # Error fragment renders at 200 (HTMX outerHTML requires 200), not a 500.
        assert response.status_code == 200

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM applications WHERE job_id = ?", (dedup_key,)).fetchone()
        assert row is None, "no package should be persisted when tailoring fails"
        conn.close()
