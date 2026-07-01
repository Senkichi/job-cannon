"""Real-path tests for submit orchestrator spine (issue #604).

These tests exercise the actual guards without mocking away the seam under test.
Every guard has a test that proves the guard FIRES on bad input (not just that
the happy path works). This is critical for fraud-surface protection.
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from job_finder.json_utils import utc_now_iso
from job_finder.web.submit_orchestrator import SubmitResult, submit_application_for


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
def app_with_submit_setup(_migrated_template_db, tmp_path):
    """Flask app with fully-migrated DB, seeded job, and pending application."""
    from job_finder.web import create_app

    path = _clone_template(_migrated_template_db)

    # Seed job with jd_full and source_url
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    jd_full = """We are looking for a Senior Data Scientist to join our team. You will be responsible for building machine learning models, analyzing large datasets, and collaborating with cross-functional teams to drive data-driven decisions. The ideal candidate has strong Python skills, experience with deep learning frameworks, and a passion for solving complex problems. This is a remote position with competitive compensation and benefits. You will work on cutting-edge projects that directly impact our business and customers. Our team values innovation, collaboration, and continuous learning. We offer a flexible work environment, professional development opportunities, and a comprehensive benefits package including health insurance, retirement plans, and generous vacation time. Join us in building the future of data science at our company."""
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description, jd_full,
             first_seen, last_seen, score_breakdown, user_interest, pipeline_status,
             direct_url, direct_url_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test|job|remote",
            "Test Job",
            "Test Company",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/view/123"]',
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
            "https://company.com/careers/123",  # strict ATS URL
            "strict",
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

    yield application, path


def _seed_pending_application(conn: sqlite3.Connection, job_id: str) -> int:
    """Seed a pending application row for a job. Return the application_id."""
    from job_finder.db._applications import upsert_application

    application_id = upsert_application(
        conn=conn,
        job_id=job_id,
        resume_content='{"summary": "test"}',
        form_mapping={"apply_url": "https://company.com/careers/123", "full_name": "Test User"},
        drafted_answers={"Why do you want to work here?": "Test answer"},
    )
    return application_id


def _get_application_row(conn: sqlite3.Connection, application_id: int) -> dict:
    """Get application row via production accessor (parses form_mapping_json)."""
    from job_finder.db import get_application

    row = get_application(conn, application_id)
    if row is None:
        raise ValueError(f"Application {application_id} not found")
    return dict(row)


def test_submit_gate_default_off(app_with_submit_setup):
    """Config gate default OFF: returns DISABLED, no ledger row, seam never called."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job and application
    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit disabled (default)
    config = app.config.copy()
    config["application"] = {"auto_submit": {"enabled": False}}

    # Mock the seam to ensure it's never called
    with patch("job_finder.web.submit_orchestrator.submit_application") as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: DISABLED outcome
        assert result.outcome == "disabled"
        assert result.reason == "Auto-submit disabled in config"

        # ASSERT: Seam was never called
        mock_submit.assert_not_called()

        # ASSERT: No ledger row written
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        assert ledger_count == 0

    conn.close()


def test_submit_gate_default_off_no_enabled_key(app_with_submit_setup):
    """Config gate default OFF: no 'enabled' key → DISABLED, no ledger row, seam never called."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job and application
    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit present but no 'enabled' key (default OFF)
    config = app.config.copy()
    config["application"] = {"auto_submit": {}}

    # Mock the seam to ensure it's never called
    with patch("job_finder.web.submit_orchestrator.submit_application") as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: DISABLED outcome
        assert result.outcome == "disabled"
        assert result.reason == "Auto-submit disabled in config"

        # ASSERT: Seam was never called
        mock_submit.assert_not_called()

        # ASSERT: No ledger row written
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        assert ledger_count == 0

    conn.close()


def test_submit_gate_default_off_no_auto_submit_key(app_with_submit_setup):
    """Config gate default OFF: no 'auto_submit' key → DISABLED, no ledger row, seam never called."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job and application
    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with application present but no 'auto_submit' key (default OFF)
    config = app.config.copy()
    config["application"] = {}

    # Mock the seam to ensure it's never called
    with patch("job_finder.web.submit_orchestrator.submit_application") as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: DISABLED outcome
        assert result.outcome == "disabled"
        assert result.reason == "Auto-submit disabled in config"

        # ASSERT: Seam was never called
        mock_submit.assert_not_called()

        # ASSERT: No ledger row written
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        assert ledger_count == 0

    conn.close()


def test_submit_gate_default_off_no_application_key(app_with_submit_setup):
    """Config gate default OFF: no 'application' key → DISABLED, no ledger row, seam never called."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job and application
    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with no 'application' key at all (default OFF)
    config = app.config.copy()
    config = {}

    # Mock the seam to ensure it's never called
    with patch("job_finder.web.submit_orchestrator.submit_application") as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: DISABLED outcome
        assert result.outcome == "disabled"
        assert result.reason == "Auto-submit disabled in config"

        # ASSERT: Seam was never called
        mock_submit.assert_not_called()

        # ASSERT: No ledger row written
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        assert ledger_count == 0

    conn.close()


def test_target_url_safety_refuses_aggregator(app_with_submit_setup):
    """Target-URL safety: aggregator/non-strict apply_url → refused, seam never invoked.

    This test specifically targets the is_ats_or_careers_url branch: the apply_url is
    a LinkedIn aggregator URL, which should be refused even if the job's direct_url_confidence
    is 'strict' (because the apply_url is NOT the direct_url).
    """
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job with strict ATS direct_url (but apply_url will be aggregator)
    job_id = "test|job|remote"
    conn.execute(
        """UPDATE jobs SET direct_url = 'https://company.com/careers/123',
                         direct_url_confidence = 'strict'
           WHERE dedup_key = ?""",
        (job_id,),
    )
    conn.commit()

    # Seed application with aggregator apply_url (DIFFERENT from direct_url)
    application_id = _seed_pending_application(conn, job_id)
    conn.execute(
        """UPDATE applications SET form_mapping_json = ?
           WHERE id = ?""",
        (json.dumps({"apply_url": "https://linkedin.com/jobs/view/123"}), application_id),
    )
    conn.commit()

    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled and require_strict_target (default)
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Mock the seam to ensure it's never called
    with patch("job_finder.web.submit_orchestrator.submit_application") as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: REFUSED outcome with non_strict_target reason
        assert result.outcome == "refused"
        assert "non-strict target" in result.reason.lower()

        # ASSERT: Seam was never called
        mock_submit.assert_not_called()

        # ASSERT: Ledger row written with refused outcome
        ledger_row = conn.execute(
            "SELECT * FROM submit_attempts WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        assert ledger_row is not None
        assert ledger_row["outcome"] == "refused"
        assert "non-strict target" in ledger_row["detail"].lower()

    conn.close()


def test_target_url_safety_accepts_strict_ats(app_with_submit_setup):
    """Target-URL safety: strict ATS apply_url → passes safety gate (reaches seam).

    This test specifically targets the is_ats_or_careers_url branch: the apply_url is
    a Greenhouse ATS URL, which should pass even if it's NOT the job's direct_url.
    """
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Seed job with NO direct_url (apply_url is standalone ATS URL)
    job_id = "test|job|remote"
    conn.execute(
        """UPDATE jobs SET direct_url = NULL, direct_url_confidence = NULL
           WHERE dedup_key = ?""",
        (job_id,),
    )
    conn.commit()

    # Seed application with strict ATS apply_url (Greenhouse)
    application_id = _seed_pending_application(conn, job_id)
    conn.execute(
        """UPDATE applications SET form_mapping_json = ?
           WHERE id = ?""",
        (json.dumps({"apply_url": "https://boards.greenhouse.io/acme/jobs/123"}), application_id),
    )
    conn.commit()

    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled and require_strict_target (default)
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Mock the seam to ensure it IS called
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="not_wired"),
    ) as mock_submit:
        result = submit_application_for(conn, config, application_row)

        # ASSERT: Seam WAS called (safety gate passed via is_ats_or_careers_url)
        mock_submit.assert_called_once()

        # ASSERT: Outcome is not_wired (default seam), not refused
        assert result.outcome == "not_wired"

    conn.close()


def test_idempotency_no_double_submit(app_with_submit_setup):
    """Idempotency: already-submitted job refused, seam not called twice.

    Tests the re-prepare path: after first submission, re-prepare (upsert_application)
    resets status to 'pending', but the immutable ledger still blocks double submit.
    """
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Mock seam to return 'submitted'
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="submitted"),
    ):
        # First submit should succeed
        result1 = submit_application_for(conn, config, application_row)
        assert result1.outcome == "submitted"

        # Verify application is now 'submitted'
        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        assert app_row["status"] == "submitted"

        # Simulate re-prepare: upsert_application resets status to 'pending'
        from job_finder.db._applications import upsert_application

        upsert_application(
            conn=conn,
            job_id=job_id,
            resume_content='{"summary": "test"}',
            form_mapping={
                "apply_url": "https://company.com/careers/123",
                "full_name": "Test User",
            },
            drafted_answers={"Why do you want to work here?": "Test answer"},
        )

        # Verify status is now 'pending' (re-prepare worked)
        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        assert app_row["status"] == "pending"

        # Reload application row via production accessor
        application_row = _get_application_row(conn, application_id)

        # Second submit should STILL be refused (immutable ledger guard)
        result2 = submit_application_for(conn, config, application_row)
        assert result2.outcome == "refused"
        assert "already submitted" in result2.reason.lower()

    conn.close()


def test_rate_limit_survives_restart(app_with_submit_setup):
    """Rate limit: DB-reconstructed count survives restart, refuses at cap."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with daily_limit = 2
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 2}
    }

    # Seed 2 submit_attempts in the ledger for DIFFERENT jobs (simulating prior submissions today)
    # Use different job_ids to avoid triggering the idempotency guard
    conn.execute(
        """INSERT INTO submit_attempts (job_id, mechanism, apply_url, target_confidence, outcome, detail, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "other|job|1",
            "extension",
            "https://company.com/careers/123",
            "strict",
            "submitted",
            "test",
            utc_now_iso(),
        ),
    )
    conn.execute(
        """INSERT INTO submit_attempts (job_id, mechanism, apply_url, target_confidence, outcome, detail, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "other|job|2",
            "extension",
            "https://company.com/careers/456",
            "strict",
            "submitted",
            "test",
            utc_now_iso(),
        ),
    )
    conn.commit()

    # Mock seam
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="submitted"),
    ):
        # Next submit should be refused (rate limit)
        result = submit_application_for(conn, config, application_row)
        assert result.outcome == "refused"
        assert "rate limit" in result.reason.lower()

    conn.close()


def test_rate_limit_ignores_refusals(app_with_submit_setup):
    """Rate limit: refusals do NOT consume the daily cap (only dispatched/submitted count)."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with daily_limit = 1
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 1}
    }

    # Seed 5 refused attempts in the ledger (should NOT consume cap)
    for i in range(5):
        conn.execute(
            """INSERT INTO submit_attempts (job_id, mechanism, apply_url, target_confidence, outcome, detail, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{job_id}-{i}",
                "extension",
                "https://linkedin.com/jobs/view/123",
                None,
                "refused",
                "test refusal",
                utc_now_iso(),
            ),
        )
    conn.commit()

    # Mock seam to return 'submitted'
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="submitted"),
    ):
        # Next submit should SUCCEED (refusals don't count against cap)
        result = submit_application_for(conn, config, application_row)
        assert result.outcome == "submitted"

    conn.close()


def test_ledger_records_failure(app_with_submit_setup):
    """Audit ledger: failing seam still writes exactly one immutable submit_attempts row."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Mock seam to raise an exception
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        side_effect=RuntimeError("Submit mechanism failed"),
    ):
        result = submit_application_for(conn, config, application_row)

        # ASSERT: failed outcome
        assert result.outcome == "failed"
        assert "exception" in result.reason.lower()

        # ASSERT: Exactly one ledger row written with outcome='failed'
        ledger_rows = conn.execute(
            "SELECT * FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchall()
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["outcome"] == "failed"
        assert "exception" in ledger_rows[0]["detail"].lower()

        # ASSERT: Application resolved to 'submit_failed'
        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        assert app_row["status"] == "submit_failed"

    conn.close()


def test_approve_flips_applied_only_on_real_submit(app_with_submit_setup):
    """Approve route: only flips pipeline_status to 'applied' on real 'submitted' outcome.

    Guards the fraud surface: on refusal/failure/not_wired, pipeline_status UNCHANGED
    and application resolves to 'submit_failed'.
    """
    app, db_path = app_with_submit_setup
    client = app.test_client()

    # Enable auto_submit in JF_CONFIG for this test (the route reads from JF_CONFIG)
    app.config["JF_CONFIG"]["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": False, "daily_limit": 5}
    }

    job_id = "test|job|remote"

    # Test 1: Seam succeeds → pipeline_status flips to 'applied'
    application_id = _seed_pending_application(sqlite3.connect(db_path), job_id)

    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="submitted"),
    ):
        response = client.post(f"/jobs/applications/{application_id}/approve")
        assert response.status_code == 200

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        job_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        assert job_row["pipeline_status"] == "applied"

        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        assert app_row["status"] == "submitted"
        conn.close()

    # Test 2: Seam fails → pipeline_status UNCHANGED, application 'submit_failed'
    # Use a different job for this test to avoid UNIQUE constraint conflicts
    job_id_2 = "test|job|remote|2"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    jd_full = """We are looking for a Senior Data Scientist to join our team. You will be responsible for building machine learning models, analyzing large datasets, and collaborating with cross-functional teams to drive data-driven decisions. The ideal candidate has strong Python skills, experience with deep learning frameworks, and a passion for solving complex problems. This is a remote position with competitive compensation and benefits. You will work on cutting-edge projects that directly impact our business and customers. Our team values innovation, collaboration, and continuous learning. We offer a flexible work environment, professional development opportunities, and a comprehensive benefits package including health insurance, retirement plans, and generous vacation time. Join us in building the future of data science at our company."""
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description, jd_full,
             first_seen, last_seen, score_breakdown, user_interest, pipeline_status,
             direct_url, direct_url_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id_2,
            "Test Job 2",
            "Test Company",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/view/456"]',
            "67890",
            150000,
            200000,
            "Test description",
            jd_full,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
            "{}",
            "interested",
            "new",
            "https://company.com/careers/456",
            "strict",
        ),
    )
    conn.commit()
    conn.close()

    application_id_2 = _seed_pending_application(sqlite3.connect(db_path), job_id_2)

    # Update the application's apply_url to match the job's direct_url (strict ATS)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE applications SET form_mapping_json = ?
           WHERE id = ?""",
        (
            json.dumps({"apply_url": "https://company.com/careers/456", "full_name": "Test User"}),
            application_id_2,
        ),
    )
    conn.commit()
    conn.close()

    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="not_wired", reason="Mechanism not wired"),
    ):
        # Re-enable auto_submit in JF_CONFIG (it may have been disabled by the first test's outcome)
        app.config["JF_CONFIG"]["application"] = {
            "auto_submit": {"enabled": True, "require_strict_target": False, "daily_limit": 5}
        }
        response = client.post(f"/jobs/applications/{application_id_2}/approve")
        assert response.status_code == 200

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        job_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id_2,)
        ).fetchone()
        assert job_row["pipeline_status"] == "new"  # UNCHANGED

        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id_2,)
        ).fetchone()
        assert app_row["status"] == "submit_failed"
        conn.close()


def test_dispatched_outcome_handling(app_with_submit_setup):
    """Dispatched outcome: application status becomes 'dispatched', pipeline_status UNCHANGED."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Mock seam to return 'dispatched'
    with patch(
        "job_finder.web.submit_orchestrator.submit_application",
        return_value=SubmitResult(outcome="dispatched"),
    ):
        result = submit_application_for(conn, config, application_row)

        # ASSERT: dispatched outcome
        assert result.outcome == "dispatched"

        # ASSERT: Application status becomes 'dispatched'
        app_row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        assert app_row["status"] == "dispatched"

        # ASSERT: Exactly one ledger row with outcome='dispatched'
        ledger_rows = conn.execute(
            "SELECT * FROM submit_attempts WHERE job_id = ?", (job_id,)
        ).fetchall()
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["outcome"] == "dispatched"

    conn.close()


def test_submit_orchestrator_default_noop(app_with_submit_setup):
    """Default submit_application seam is a no-op returning 'not_wired'."""
    app, db_path = app_with_submit_setup
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    job_id = "test|job|remote"
    application_id = _seed_pending_application(conn, job_id)
    application_row = _get_application_row(conn, application_id)

    # Config with auto_submit enabled
    config = app.config.copy()
    config["application"] = {
        "auto_submit": {"enabled": True, "require_strict_target": True, "daily_limit": 5}
    }

    # Call the actual default seam (unpatched)
    result = submit_application_for(conn, config, application_row)

    # ASSERT: not_wired outcome (default no-op)
    assert result.outcome == "not_wired"
    assert result.reason == "Submit mechanism not wired"

    # ASSERT: Ledger row written with not_wired outcome
    ledger_row = conn.execute(
        "SELECT * FROM submit_attempts WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    assert ledger_row is not None
    assert ledger_row["outcome"] == "not_wired"

    # ASSERT: Application resolved to 'submit_failed'
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    assert app_row["status"] == "submit_failed"

    conn.close()
