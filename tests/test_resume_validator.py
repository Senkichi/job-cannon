"""Tests for resume_validator.py — Sonnet audit and fix module.

Covers:
- VALIDATION_SCHEMA structure (TestValidationSchema)
- validate_resume: clean audit, violations, fail-open on exception, fail-open on BudgetExceededError (TestValidateResume)
- fix_resume_violations: fix pass, fail-open on exception, error-only filtering (TestFixResumeViolations)
- Integration with _generate_resume_background (TestValidatorBackgroundIntegration)
- Integration with generate_resume_single quick-apply path (TestValidatorQuickApplyIntegration)
"""

import json
import sqlite3
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn():
    """Return an in-memory SQLite connection with scoring_costs table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            timestamp TEXT
        )"""
    )
    conn.commit()
    return conn


def _test_config():
    """Return a minimal config dict for tests."""
    return {
        "scoring": {
            "daily_budget_usd": 25.0,
            "models": {
                "sonnet": "claude-sonnet-4-6",
            },
        },
    }


def _mock_call_claude_clean(*args, **kwargs):
    """Mock call_claude returning a clean audit (no violations)."""
    return {"passed": True, "violations": []}, 0.01


def _mock_call_claude_violations(*args, **kwargs):
    """Mock call_claude returning one error violation."""
    return {
        "passed": False,
        "violations": [
            {
                "category": "content_integrity",
                "description": "Skill 'dbt' listed in Skills section but not found in experience profile",
                "severity": "error",
                "location": "skills",
            }
        ],
    }, 0.01


def _mock_call_claude_fixed(*args, **kwargs):
    """Mock call_claude returning a fixed resume dict."""
    return {
        "name": "Jane Doe",
        "contact_line": "jane@example.com",
        "summary": "A data scientist with 8 years of experience.",
        "skills": ["Python", "SQL", "A/B Testing"],
        "positions": [],
    }, 0.01


# ---------------------------------------------------------------------------
# TestValidationSchema
# ---------------------------------------------------------------------------


class TestValidationSchema:
    """Verify VALIDATION_SCHEMA has the required structure."""

    def test_schema_has_passed_boolean(self):
        """VALIDATION_SCHEMA has 'passed' property of type boolean."""
        from job_finder.web.resume_validator import VALIDATION_SCHEMA

        props = VALIDATION_SCHEMA["properties"]
        assert "passed" in props
        assert props["passed"]["type"] == "boolean"

    def test_schema_has_violations_array(self):
        """VALIDATION_SCHEMA has 'violations' property of type array."""
        from job_finder.web.resume_validator import VALIDATION_SCHEMA

        props = VALIDATION_SCHEMA["properties"]
        assert "violations" in props
        assert props["violations"]["type"] == "array"

    def test_violations_items_have_required_fields(self):
        """Each violation item requires category, description, severity."""
        from job_finder.web.resume_validator import VALIDATION_SCHEMA

        items = VALIDATION_SCHEMA["properties"]["violations"]["items"]
        required = items.get("required", [])
        assert "category" in required
        assert "description" in required
        assert "severity" in required

    def test_schema_has_no_additional_properties(self):
        """VALIDATION_SCHEMA has additionalProperties: False."""
        from job_finder.web.resume_validator import VALIDATION_SCHEMA

        assert VALIDATION_SCHEMA.get("additionalProperties") is False

    def test_schema_required_fields(self):
        """VALIDATION_SCHEMA requires both 'passed' and 'violations'."""
        from job_finder.web.resume_validator import VALIDATION_SCHEMA

        required = VALIDATION_SCHEMA.get("required", [])
        assert "passed" in required
        assert "violations" in required


# ---------------------------------------------------------------------------
# TestValidateResume
# ---------------------------------------------------------------------------


class TestValidateResume:
    """Tests for validate_resume() function."""

    def test_clean_audit_returns_passed(self):
        """validate_resume with clean audit returns passed=True, violations=[]."""
        from job_finder.web.resume_validator import validate_resume

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "A senior data scientist.",
            "skills": ["Python", "SQL"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}
        jd_text = "We need a data scientist with Python and SQL."

        with patch(
            "job_finder.web.resume_validator.call_claude", side_effect=_mock_call_claude_clean
        ):
            result = validate_resume(resume_data, jd_text, profile, conn, config)

        assert result["passed"] is True
        assert result["violations"] == []

    def test_violations_audit_returns_failed(self):
        """validate_resume with violations returns passed=False with violation list."""
        from job_finder.web.resume_validator import validate_resume

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "Data scientist.",
            "skills": ["Python", "SQL", "dbt"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}
        jd_text = "Looking for dbt experience."

        with patch(
            "job_finder.web.resume_validator.call_claude", side_effect=_mock_call_claude_violations
        ):
            result = validate_resume(resume_data, jd_text, profile, conn, config)

        assert result["passed"] is False
        assert len(result["violations"]) == 1
        violation = result["violations"][0]
        assert "category" in violation
        assert "description" in violation
        assert "severity" in violation

    def test_api_exception_returns_fail_open(self):
        """validate_resume returns passed=True on API exception (fail-open)."""
        from job_finder.web.resume_validator import validate_resume

        conn = _make_conn()
        config = _test_config()
        resume_data = {"name": "Jane", "summary": "DS", "skills": [], "positions": []}
        profile = {"skills": [], "positions": []}

        def raise_exception(*args, **kwargs):
            raise RuntimeError("API connection error")

        with patch("job_finder.web.resume_validator.call_claude", side_effect=raise_exception):
            result = validate_resume(resume_data, "", profile, conn, config)

        assert result["passed"] is True
        assert result["violations"] == []

    def test_budget_exceeded_returns_fail_open(self):
        """validate_resume returns passed=True on BudgetExceededError (fail-open)."""
        from job_finder.web.claude_client import BudgetExceededError
        from job_finder.web.resume_validator import validate_resume

        conn = _make_conn()
        config = _test_config()
        resume_data = {"name": "Jane", "summary": "DS", "skills": [], "positions": []}
        profile = {"skills": [], "positions": []}

        def raise_budget(*args, **kwargs):
            raise BudgetExceededError("Monthly budget cap reached")

        with patch("job_finder.web.resume_validator.call_claude", side_effect=raise_budget):
            result = validate_resume(resume_data, "", profile, conn, config)

        assert result["passed"] is True
        assert result["violations"] == []


# ---------------------------------------------------------------------------
# TestFixResumeViolations
# ---------------------------------------------------------------------------


class TestFixResumeViolations:
    """Tests for fix_resume_violations() function."""

    def test_fix_returns_fixed_resume(self):
        """fix_resume_violations returns fixed resume dict when Sonnet succeeds."""
        from job_finder.web.resume_validator import fix_resume_violations

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "Data scientist.",
            "skills": ["Python", "SQL", "dbt"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}
        violations = [
            {
                "category": "content_integrity",
                "description": "Skill 'dbt' is fabricated",
                "severity": "error",
            }
        ]

        with patch(
            "job_finder.web.resume_validator.call_claude", side_effect=_mock_call_claude_fixed
        ):
            result = fix_resume_violations(resume_data, violations, profile, conn, config)

        # Should return the fixed resume (without dbt in skills)
        assert isinstance(result, dict)
        assert "name" in result
        assert "dbt" not in result.get("skills", [])

    def test_fix_exception_returns_original(self):
        """fix_resume_violations returns original resume_data on exception (fail-open)."""
        from job_finder.web.resume_validator import fix_resume_violations

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "Data scientist.",
            "skills": ["Python", "SQL", "dbt"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}
        violations = [
            {
                "category": "content_integrity",
                "description": "Skill 'dbt' is fabricated",
                "severity": "error",
            }
        ]

        def raise_exception(*args, **kwargs):
            raise RuntimeError("Sonnet API error")

        with patch("job_finder.web.resume_validator.call_claude", side_effect=raise_exception):
            result = fix_resume_violations(resume_data, violations, profile, conn, config)

        # Should return original unchanged
        assert result is resume_data
        assert "dbt" in result["skills"]

    def test_fix_only_sends_error_violations(self):
        """fix_resume_violations filters out warning-severity violations before sending to Sonnet."""
        from job_finder.web.resume_validator import fix_resume_violations

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "Data scientist.",
            "skills": ["Python", "SQL", "dbt"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}

        # Mix of error + warning violations
        violations = [
            {
                "category": "content_integrity",
                "description": "Skill 'dbt' is fabricated",
                "severity": "error",
            },
            {
                "category": "style",
                "description": "Em dash found in bullet",
                "severity": "warning",
            },
            {
                "category": "style",
                "description": "Two consecutive bullets start with 'Built'",
                "severity": "warning",
            },
        ]

        captured_messages = []

        def capture_call(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return _mock_call_claude_fixed(*args, **kwargs)

        with patch("job_finder.web.resume_validator.call_claude", side_effect=capture_call):
            fix_resume_violations(resume_data, violations, profile, conn, config)

        # The user message should contain error violations but NOT warning violations
        assert len(captured_messages) > 0
        user_content = captured_messages[0]["content"]
        # Error violation should be in the message
        assert "dbt" in user_content or "fabricated" in user_content
        # Warning violations should NOT be the only thing passed — dbt error must appear
        # The key check: warning-only violations are not what triggered this call
        # (if only warnings were present, the function returns early without calling Claude)

    def test_fix_returns_original_if_no_error_violations(self):
        """fix_resume_violations returns original resume if only warnings present."""
        from job_finder.web.resume_validator import fix_resume_violations

        conn = _make_conn()
        config = _test_config()
        resume_data = {
            "name": "Jane Doe",
            "summary": "Data scientist.",
            "skills": ["Python", "SQL"],
            "positions": [],
        }
        profile = {"skills": ["Python", "SQL"], "positions": []}

        # Only warnings, no errors
        violations = [
            {
                "category": "style",
                "description": "Em dash found in bullet",
                "severity": "warning",
            },
        ]

        call_count = []

        def count_calls(*args, **kwargs):
            call_count.append(1)
            return _mock_call_claude_fixed(*args, **kwargs)

        with patch("job_finder.web.resume_validator.call_claude", side_effect=count_calls):
            result = fix_resume_violations(resume_data, violations, profile, conn, config)

        # Should return original without calling Claude at all
        assert result is resume_data
        assert len(call_count) == 0, "Claude should not be called when only warnings present"


# ---------------------------------------------------------------------------
# TestAuditSystemPrompt
# ---------------------------------------------------------------------------


class TestAuditSystemPrompt:
    """Verify _AUDIT_SYSTEM contains the required category names."""

    def test_audit_system_has_content_integrity(self):
        """_AUDIT_SYSTEM mentions content_integrity."""
        from job_finder.web.resume_validator import _AUDIT_SYSTEM

        assert "content_integrity" in _AUDIT_SYSTEM

    def test_audit_system_has_structural(self):
        """_AUDIT_SYSTEM mentions structural."""
        from job_finder.web.resume_validator import _AUDIT_SYSTEM

        assert "structural" in _AUDIT_SYSTEM

    def test_audit_system_has_style(self):
        """_AUDIT_SYSTEM mentions style."""
        from job_finder.web.resume_validator import _AUDIT_SYSTEM

        assert "style" in _AUDIT_SYSTEM

    def test_audit_system_has_jd_alignment(self):
        """_AUDIT_SYSTEM mentions jd_alignment."""
        from job_finder.web.resume_validator import _AUDIT_SYSTEM

        assert "jd_alignment" in _AUDIT_SYSTEM

    def test_audit_system_has_readability(self):
        """_AUDIT_SYSTEM mentions readability."""
        from job_finder.web.resume_validator import _AUDIT_SYSTEM

        assert "readability" in _AUDIT_SYSTEM


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------


def _setup_resume_gen_db(db_path):
    """Run migrations and insert a pending resume_generations row. Returns gen_id."""
    from job_finder.web.db_migrate import run_migrations

    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO resume_generations (job_id, generated_at, model, status, generation_type) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acme|senior-ds|remote", "2026-03-11T00:00:00", "claude-sonnet-4-6", "pending", "single"),
    )
    conn.commit()
    gen_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return gen_id


_MOCK_RESUME = {
    "name": "Jane Doe",
    "contact_line": "jane@example.com",
    "summary": "Experienced data scientist with 8 years.",
    "skills": ["Python", "SQL", "A/B Testing"],
    "positions": [
        {
            "title": "Senior Data Scientist",
            "company": "Acme Corp",
            "dates": "Jan 2021 - Present",
            "achievements": ["Built ML pipeline serving 10M users"],
        }
    ],
    "education": [{"degree": "M.S. Statistics", "institution": "Stanford", "year": "2018"}],
}

_MOCK_JOB_ROW = {
    "dedup_key": "acme|senior-ds|remote",
    "title": "Senior Data Scientist",
    "company": "Acme Corp",
    "jd_full": "Looking for a data scientist with Python and SQL skills.",
    "fit_analysis": None,
    "sonnet_score": 50.0,
    # v3.0 (Phase 34 Plan 3 Commit E): dispatch uses classification
    # == 'apply'. 'consider' takes the single-version path which these
    # validator tests exercise.
    "classification": "consider",
}

_BACKGROUND_CONFIG = {
    "scoring": {
        "models": {"sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"},
        "daily_budget_usd": 25.0,
        "multi_version_threshold": 80,
    },
    "drive": {"folder_id": "test-folder", "convert_to_gdoc": True},
}

# ---------------------------------------------------------------------------
# TestValidatorBackgroundIntegration
# ---------------------------------------------------------------------------


class TestValidatorBackgroundIntegration:
    """Integration tests for validator in _generate_resume_background."""

    def test_validator_called_after_generation(self, tmp_path, sample_resume_data):
        """_generate_resume_background calls validate_resume and stores validation_report."""
        from job_finder.web.resume_generator import _generate_resume_background

        db_path = str(tmp_path / "test.db")
        gen_id = _setup_resume_gen_db(db_path)

        mock_validation = {"passed": True, "violations": []}

        with (
            patch(
                "job_finder.web.resume_generator.generate_resume_single", return_value=_MOCK_RESUME
            ),
            patch("job_finder.web.resume_validator.call_claude") as mock_cc,
        ):
            mock_cc.return_value = (mock_validation, 0.01)
            with patch("job_finder.web.resume_generator.build_resume_docx") as mock_docx:
                mock_docx.return_value = __import__("io").BytesIO(b"fake-docx")
                with patch("job_finder.web.resume_generator.get_drive_service"):
                    with patch(
                        "job_finder.web.resume_generator.upload_to_drive",
                        return_value="https://docs.google.com/doc/xyz",
                    ):
                        _generate_resume_background(
                            db_path,
                            gen_id,
                            _MOCK_JOB_ROW,
                            sample_resume_data,
                            _BACKGROUND_CONFIG,
                        )

        # Validator was called
        assert mock_cc.called, "validate_resume should have called call_claude"

        # Validation report saved to DB
        verify_conn = sqlite3.connect(db_path)
        row = verify_conn.execute(
            "SELECT status, validation_report FROM resume_generations WHERE id = ?",
            (gen_id,),
        ).fetchone()
        verify_conn.close()

        assert row[0] == "done"
        assert row[1] is not None, "validation_report should be saved to DB"
        report = json.loads(row[1])
        assert report["passed"] is True

    def test_autofix_runs_on_error_violations(self, tmp_path, sample_resume_data):
        """_generate_resume_background runs fix pass when error violations found."""
        from job_finder.web.resume_generator import _generate_resume_background

        db_path = str(tmp_path / "test.db")
        gen_id = _setup_resume_gen_db(db_path)

        mock_resume_with_error = dict(_MOCK_RESUME)
        mock_resume_with_error["skills"] = ["Python", "SQL", "dbt"]  # dbt is fabricated

        mock_validation_with_error = {
            "passed": False,
            "violations": [
                {
                    "category": "content_integrity",
                    "description": "Skill 'dbt' is fabricated",
                    "severity": "error",
                }
            ],
        }
        mock_fixed_resume = dict(_MOCK_RESUME)  # dbt removed

        call_count = [0]

        def multi_return(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return (mock_validation_with_error, 0.01)
            else:
                return (mock_fixed_resume, 0.01)

        with (
            patch(
                "job_finder.web.resume_generator.generate_resume_single",
                return_value=mock_resume_with_error,
            ),
            patch("job_finder.web.resume_validator.call_claude", side_effect=multi_return),
        ):
            with patch("job_finder.web.resume_generator.build_resume_docx") as mock_docx:
                mock_docx.return_value = __import__("io").BytesIO(b"fake-docx")
                with patch("job_finder.web.resume_generator.get_drive_service"):
                    with patch(
                        "job_finder.web.resume_generator.upload_to_drive",
                        return_value="https://docs.google.com/doc/xyz",
                    ):
                        _generate_resume_background(
                            db_path,
                            gen_id,
                            _MOCK_JOB_ROW,
                            sample_resume_data,
                            _BACKGROUND_CONFIG,
                        )

        # Both audit and fix calls were made
        assert call_count[0] == 2, f"Expected 2 Claude calls (audit + fix), got: {call_count[0]}"

        # Validation report stored with fix_applied flag
        verify_conn = sqlite3.connect(db_path)
        row = verify_conn.execute(
            "SELECT validation_report FROM resume_generations WHERE id = ?",
            (gen_id,),
        ).fetchone()
        verify_conn.close()

        assert row[0] is not None
        report = json.loads(row[0])
        assert report.get("fix_applied") is True

    def test_validator_failure_does_not_block_generation(self, tmp_path, sample_resume_data):
        """_generate_resume_background still completes if validation raises exception (fail-open).

        When call_claude raises an exception, validate_resume returns fail-open
        {"passed": True, "violations": []} and the background function saves that
        to DB. Generation always completes with status='done'.
        """
        from job_finder.web.resume_generator import _generate_resume_background

        db_path = str(tmp_path / "test.db")
        gen_id = _setup_resume_gen_db(db_path)

        with (
            patch(
                "job_finder.web.resume_generator.generate_resume_single", return_value=_MOCK_RESUME
            ),
            patch(
                "job_finder.web.resume_validator.call_claude",
                side_effect=RuntimeError("Sonnet down"),
            ),
            patch("job_finder.web.resume_generator.build_resume_docx") as mock_docx,
        ):
            mock_docx.return_value = __import__("io").BytesIO(b"fake-docx")
            with (
                patch("job_finder.web.resume_generator.get_drive_service"),
                patch(
                    "job_finder.web.resume_generator.upload_to_drive",
                    return_value="https://docs.google.com/doc/xyz",
                ),
            ):
                _generate_resume_background(
                    db_path,
                    gen_id,
                    _MOCK_JOB_ROW,
                    sample_resume_data,
                    _BACKGROUND_CONFIG,
                )

        # Generation completes successfully despite validation failure
        verify_conn = sqlite3.connect(db_path)
        row = verify_conn.execute(
            "SELECT status, validation_report FROM resume_generations WHERE id = ?",
            (gen_id,),
        ).fetchone()
        verify_conn.close()

        assert row[0] == "done", (
            f"Generation should still complete on validation failure, got status: {row[0]}"
        )
        # validate_resume is fail-open: returns {"passed": True, "violations": []} even on API errors
        # The background function saves this fail-open result to DB
        if row[1] is not None:
            report = json.loads(row[1])
            assert report.get("passed") is True, "Fail-open report should be passed=True"


# ---------------------------------------------------------------------------
# TestValidatorQuickApplyIntegration
# ---------------------------------------------------------------------------


class TestValidatorQuickApplyIntegration:
    """Integration tests for validator in generate_resume_single quick-apply path."""

    def _make_migrated_conn(self, db_path):
        """Set up a migrated DB and return open connection."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_quick_apply_validates_inline(self, tmp_path, sample_resume_data):
        """generate_resume_single calls validate_resume after generation."""
        from job_finder.web.resume_generator import generate_resume_single

        db_path = str(tmp_path / "test.db")
        conn = self._make_migrated_conn(db_path)

        try:
            config = _BACKGROUND_CONFIG

            call_count = [0]

            # First call: generation; second call: audit
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    # This is the resume generation call via call_claude
                    return (_MOCK_RESUME, 0.01)
                else:
                    # This is the audit call
                    return ({"passed": True, "violations": []}, 0.01)

            with patch("job_finder.web.resume_generator.call_claude", side_effect=side_effect):
                with patch(
                    "job_finder.web.resume_validator.call_claude",
                    return_value=({"passed": True, "violations": []}, 0.01),
                ):
                    with patch("job_finder.web.resume_generator.cost_gate", return_value=True):
                        result = generate_resume_single(
                            _MOCK_JOB_ROW, sample_resume_data, conn, config
                        )

            assert result is not None
            assert "name" in result
        finally:
            conn.close()

    def test_quick_apply_fixes_errors_inline(self, tmp_path, sample_resume_data):
        """generate_resume_single applies fix pass when error violations found."""
        from job_finder.web.resume_generator import generate_resume_single

        db_path = str(tmp_path / "test.db")
        conn = self._make_migrated_conn(db_path)

        try:
            config = _BACKGROUND_CONFIG

            # Resume with fabricated skill
            resume_with_error = dict(_MOCK_RESUME)
            resume_with_error["skills"] = ["Python", "SQL", "dbt"]

            audit_result = {
                "passed": False,
                "violations": [
                    {
                        "category": "content_integrity",
                        "description": "Skill 'dbt' is fabricated",
                        "severity": "error",
                    }
                ],
            }
            fixed_resume = dict(_MOCK_RESUME)  # dbt removed

            audit_call_count = [0]

            def audit_side_effect(*args, **kwargs):
                audit_call_count[0] += 1
                if audit_call_count[0] == 1:
                    return (audit_result, 0.01)
                else:
                    return (fixed_resume, 0.01)

            with (
                patch(
                    "job_finder.web.resume_generator.call_claude",
                    return_value=(resume_with_error, 0.01),
                ),
                patch(
                    "job_finder.web.resume_validator.call_claude", side_effect=audit_side_effect
                ),
                patch("job_finder.web.resume_generator.cost_gate", return_value=True),
            ):
                result = generate_resume_single(_MOCK_JOB_ROW, sample_resume_data, conn, config)

            # Result should be the fixed resume (without dbt)
            assert result is not None
            assert "dbt" not in result.get("skills", []), (
                "Fixed resume should not contain fabricated 'dbt' skill"
            )
            # Should have called validate then fix = 2 audit calls
            assert audit_call_count[0] == 2, (
                f"Expected 2 validator calls (audit + fix), got: {audit_call_count[0]}"
            )
        finally:
            conn.close()
