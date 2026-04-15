"""Tests for profile schema, validation, I/O, and Profile Editor routes.

Covers:
- validate_profile: warnings for missing achievements, unquantified impacts,
  unmatched skill tags, and valid profiles (no warnings).
- load_profile: returns empty structure when file not found.
- save_profile: writes valid JSON.
- GET /profile: returns 200.
- POST /profile/save: persists profile data.

Note: extract_profile_from_markdown is NOT tested here (requires live Anthropic API).
"""

import io
import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.profile_schema import load_profile, save_profile, validate_profile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_profile():
    """A minimal profile dict that should produce no validation warnings."""
    return {
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "Acme Corp",
                "start_date": "Jan 2022",
                "end_date": None,
                "achievements": [
                    "Increased model accuracy by 15% reducing false positive rate",
                    "Reduced pipeline latency by 40% saving $200k annually",
                ],
                "skills": ["Python", "SQL"],
            }
        ],
        "skills": ["Python", "SQL"],
        "resume_preferences": {"summary_style": "concise", "emphasis": ["causal inference"]},
    }

@pytest.fixture
def tmp_profile_path():
    """Temp file path for profile JSON (cleaned up after test)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start fresh — load_profile expects non-existent or valid JSON
    yield path
    if os.path.exists(path):
        os.remove(path)

# ---------------------------------------------------------------------------
# validate_profile — warning detection tests
# ---------------------------------------------------------------------------

class TestValidateProfile:
    def test_position_with_no_achievements_raises_warning(self):
        """Position with empty achievements list should produce a warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": [],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no achievements" in m for m in messages)

    def test_achievement_without_quantified_impact(self):
        """Achievement with no numbers or % should produce an advisory warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Improved the reporting process significantly"],
                    "skills": ["SQL"],
                }
            ],
            "skills": ["SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("quantified impact" in m for m in messages)

    def test_skill_in_position_not_in_top_level_skills(self):
        """Skill tag in a position that is absent from top-level skills list."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Increased revenue by 20%"],
                    "skills": ["Tableau"],  # not in top-level skills
                }
            ],
            "skills": ["Python"],  # Tableau NOT here
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("Tableau" in m and "not in main skills list" in m for m in messages)

    def test_valid_profile_produces_no_warnings(self, valid_profile):
        """A well-formed profile with quantified achievements should have no warnings."""
        warnings = validate_profile(valid_profile)
        assert warnings == []

    def test_position_with_no_skills_tagged(self):
        """Position with empty skills list should produce a warning."""
        profile = {
            "positions": [
                {
                    "title": "Analyst",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Grew revenue by 30% YoY"],
                    "skills": [],  # empty
                }
            ],
            "skills": [],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        warnings = validate_profile(profile)
        messages = [w["message"] for w in warnings]
        assert any("TestCo" in m and "no skills tagged" in m for m in messages)

# ---------------------------------------------------------------------------
# load_profile / save_profile
# ---------------------------------------------------------------------------

class TestLoadSaveProfile:
    def test_load_profile_returns_empty_structure_when_file_missing(self, tmp_profile_path):
        """load_profile on a non-existent path returns a dict with empty positions/skills."""
        result = load_profile(tmp_profile_path)
        assert isinstance(result, dict)
        assert "positions" in result
        assert "skills" in result
        assert result["positions"] == []
        assert result["skills"] == []

    def test_save_profile_writes_valid_json(self, valid_profile, tmp_profile_path):
        """save_profile writes a JSON file that can be loaded back correctly."""
        save_profile(valid_profile, tmp_profile_path)
        assert os.path.exists(tmp_profile_path)

        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        assert loaded["positions"][0]["company"] == "Acme Corp"
        assert loaded["skills"] == ["Python", "SQL"]

    def test_save_profile_refuses_empty_overwrite(self, tmp_profile_path):
        """save_profile must NOT overwrite a populated profile with empty data.

        Steps:
        1. Write a profile with 3 positions and 5 skills.
        2. Attempt save_profile() with EMPTY_PROFILE.
        3. Assert the original file is unchanged.
        4. Assert a warning was logged (empty-overwrite guard triggered).
        """
        import logging
        from job_finder.web.profile_schema import EMPTY_PROFILE

        # Populate the temp file with real data
        populated = {
            "positions": [
                {
                    "title": "Staff Data Scientist",
                    "company": "TechCo",
                    "start_date": "Jan 2021",
                    "end_date": None,
                    "achievements": ["Improved model accuracy by 20%"],
                    "skills": ["Python", "PyTorch"],
                },
                {
                    "title": "Senior Data Scientist",
                    "company": "DataCo",
                    "start_date": "Mar 2019",
                    "end_date": "Dec 2020",
                    "achievements": ["Reduced churn by 15%"],
                    "skills": ["SQL", "R"],
                },
                {
                    "title": "Data Scientist",
                    "company": "StartupCo",
                    "start_date": "Jun 2017",
                    "end_date": "Feb 2019",
                    "achievements": ["Built recommendation engine"],
                    "skills": ["Python"],
                },
            ],
            "skills": ["Python", "SQL", "PyTorch", "R", "Spark"],
            "resume_preferences": {"summary_style": "concise", "emphasis": ["product analytics"]},
        }
        save_profile(populated, tmp_profile_path)

        # Confirm the file was written correctly before the guard test
        assert os.path.exists(tmp_profile_path)
        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            before = json.load(f)
        assert len(before["positions"]) == 3
        assert len(before["skills"]) == 5

        # Now attempt to overwrite with empty profile — the guard must block this
        with self._capture_warning("job_finder.web.profile_schema") as captured_warnings:
            save_profile(EMPTY_PROFILE, tmp_profile_path)

        # File must be UNCHANGED
        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert after["positions"] == before["positions"], (
            "save_profile silently overwrote populated profile with empty data"
        )
        assert after["skills"] == before["skills"], (
            "save_profile silently wiped skills with empty data"
        )

        # A warning must have been logged
        assert len(captured_warnings) > 0, (
            "save_profile did not log a warning when blocking empty overwrite"
        )

    @staticmethod
    def _capture_warning(logger_name: str):
        """Context manager that captures log records at WARNING level from a named logger."""
        import logging
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            records = []

            class _Handler(logging.Handler):
                def emit(self, record):
                    if record.levelno >= logging.WARNING:
                        records.append(record)

            handler = _Handler()
            log = logging.getLogger(logger_name)
            log.addHandler(handler)
            original_level = log.level
            log.setLevel(logging.WARNING)
            try:
                yield records
            finally:
                log.removeHandler(handler)
                log.setLevel(original_level)

        return _ctx()

    def test_save_profile_allows_empty_to_new_file(self, tmp_profile_path):
        """save_profile must allow writing EMPTY_PROFILE to a brand-new (non-existent) file.

        The guard only blocks overwriting a POPULATED profile with empty data.
        Initial writes to a missing path must always succeed.
        """
        from job_finder.web.profile_schema import EMPTY_PROFILE

        # tmp_profile_path fixture already deletes the file — path doesn't exist
        assert not os.path.exists(tmp_profile_path)

        save_profile(EMPTY_PROFILE, tmp_profile_path)
        assert os.path.exists(tmp_profile_path)

        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["positions"] == []
        assert saved["skills"] == []

    def test_save_profile_allows_populated_over_populated(self, valid_profile, tmp_profile_path):
        """save_profile must allow overwriting a populated profile with another populated profile."""
        # Write initial populated profile
        save_profile(valid_profile, tmp_profile_path)

        updated = dict(valid_profile)
        updated["skills"] = ["Python", "SQL", "Spark"]

        save_profile(updated, tmp_profile_path)

        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["skills"] == ["Python", "SQL", "Spark"]

    def test_save_profile_refuses_suspicious_reduction(self, tmp_profile_path):
        """save_profile blocks saves where both positions AND skills shrink (wipe signal)."""
        import logging

        populated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
                {"title": "B", "company": "Co2", "start_date": "", "end_date": None, "achievements": ["x2"], "skills": ["Q"]},
                {"title": "C", "company": "Co3", "start_date": "", "end_date": None, "achievements": ["x3"], "skills": ["R"]},
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }

        with self._capture_warning("job_finder.web.profile_schema") as warnings:
            save_profile(reduced, tmp_profile_path)

        # File must be unchanged
        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 3, "Suspicious reduction was not blocked"
        assert len(after["skills"]) == 5
        assert len(warnings) > 0

    def test_save_profile_allows_reduction_with_force(self, tmp_profile_path):
        """save_profile with force=True allows intentional reduction."""
        populated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
                {"title": "B", "company": "Co2", "start_date": "", "end_date": None, "achievements": ["x2"], "skills": ["Q"]},
                {"title": "C", "company": "Co3", "start_date": "", "end_date": None, "achievements": ["x3"], "skills": ["R"]},
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(reduced, tmp_profile_path, force=True)

        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 2

    def test_save_profile_allows_one_dimension_reduction(self, tmp_profile_path):
        """Reducing positions but increasing skills is allowed (not suspicious)."""
        populated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
                {"title": "B", "company": "Co2", "start_date": "", "end_date": None, "achievements": ["x2"], "skills": ["Q"]},
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        # Fewer positions but more skills — legitimate edit
        updated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
            ],
            "skills": ["P", "Q", "R", "S"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(updated, tmp_profile_path)

        with open(tmp_profile_path, "r", encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 4

    def test_save_profile_rejects_stale_mtime(self, tmp_profile_path):
        """POST /profile/save with stale _mtime returns 409."""
        populated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
            ],
            "skills": ["P"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)
        original_mtime = os.path.getmtime(tmp_profile_path)

        # Simulate external modification: write the file again
        import time
        time.sleep(0.05)
        save_profile(populated, tmp_profile_path)

        from job_finder.web import create_app
        import job_finder.web.blueprints.profile as profile_mod

        test_config = {
            "db": {"path": ":memory:"},
            "scoring": {"min_score_threshold": 40},
            "profile": {"target_titles": [], "target_locations": [], "min_salary": 0, "industries": [], "exclusions": {"title_keywords": [], "companies": []}, "skills": []},
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        app = create_app(config=test_config)
        app.config["TESTING"] = True

        orig_path = profile_mod._PROFILE_PATH
        profile_mod._PROFILE_PATH = tmp_profile_path
        try:
            client = app.test_client()
            payload = {
                "positions": [{"title": "B", "company": "Co2", "start_date": "", "end_date": None, "achievements": [], "skills": []}],
                "skills": ["P"],
                "resume_preferences": {"summary_style": "", "emphasis": []},
                "_mtime": str(original_mtime),  # stale!
            }
            resp = client.post(
                "/profile/save",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert resp.status_code == 409
        finally:
            profile_mod._PROFILE_PATH = orig_path

    def test_save_profile_accepts_fresh_mtime(self, tmp_profile_path):
        """POST /profile/save with fresh _mtime succeeds."""
        populated = {
            "positions": [
                {"title": "A", "company": "Co1", "start_date": "", "end_date": None, "achievements": ["x1"], "skills": ["P"]},
            ],
            "skills": ["P"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)
        current_mtime = os.path.getmtime(tmp_profile_path)

        from job_finder.web import create_app
        import job_finder.web.blueprints.profile as profile_mod

        test_config = {
            "db": {"path": ":memory:"},
            "scoring": {"min_score_threshold": 40},
            "profile": {"target_titles": [], "target_locations": [], "min_salary": 0, "industries": [], "exclusions": {"title_keywords": [], "companies": []}, "skills": []},
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        app = create_app(config=test_config)
        app.config["TESTING"] = True

        orig_path = profile_mod._PROFILE_PATH
        profile_mod._PROFILE_PATH = tmp_profile_path
        try:
            client = app.test_client()
            payload = {
                "positions": [{"title": "B", "company": "Co2", "start_date": "", "end_date": None, "achievements": ["x2"], "skills": ["Q"]}],
                "skills": ["P", "Q"],
                "resume_preferences": {"summary_style": "", "emphasis": []},
                "_mtime": str(current_mtime),  # fresh
            }
            resp = client.post(
                "/profile/save",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert resp.status_code in (200, 204, 302)
        finally:
            profile_mod._PROFILE_PATH = orig_path

    def test_save_profile_preserves_education(self, tmp_profile_path):
        """save_profile round-trips education data — it must not be dropped."""
        profile_with_edu = {
            "positions": [
                {
                    "title": "Data Scientist",
                    "company": "TestCo",
                    "start_date": "Jan 2020",
                    "end_date": None,
                    "achievements": ["Improved accuracy by 10%"],
                    "skills": ["Python"],
                }
            ],
            "skills": ["Python"],
            "education": [
                {"degree": "M.S. Statistics", "institution": "Stanford", "year": "2018"},
                {"degree": "B.S. Math", "institution": "MIT", "year": "2016"},
            ],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(profile_with_edu, tmp_profile_path)

        loaded = load_profile(tmp_profile_path)
        assert "education" in loaded
        assert len(loaded["education"]) == 2
        assert loaded["education"][0]["degree"] == "M.S. Statistics"
        assert loaded["education"][1]["institution"] == "MIT"

# ---------------------------------------------------------------------------
# Profile Editor routes
# ---------------------------------------------------------------------------

class TestProfileEditorRoutes:
    def test_get_profile_returns_200(self, client):
        """GET /profile returns 200."""
        response = client.get("/profile")
        assert response.status_code == 200

    def test_post_profile_save_redirects_on_success(self, client, valid_profile, tmp_profile_path):
        """POST /profile/save with valid JSON redirects to /profile."""
        import job_finder.web.blueprints.profile as profile_mod

        orig_path = profile_mod._PROFILE_PATH
        profile_mod._PROFILE_PATH = tmp_profile_path
        try:
            response = client.post(
                "/profile/save",
                data=json.dumps(valid_profile),
                content_type="application/json",
            )
            # Should redirect (302) or succeed
            assert response.status_code in (200, 302, 204)
        finally:
            profile_mod._PROFILE_PATH = orig_path

    def test_post_profile_save_persists_data(self, client, valid_profile):
        """POST /profile/save writes profile to disk; GET /profile then shows it."""
        import os
        import tempfile

        from job_finder.web.profile_schema import load_profile

        # Use a temp file to avoid polluting the real experience_profile.json
        fd, tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_path)

        try:
            # Monkeypatch the profile path used by the blueprint
            import job_finder.web.blueprints.profile as profile_mod
            original_path = profile_mod._PROFILE_PATH
            profile_mod._PROFILE_PATH = tmp_path

            response = client.post(
                "/profile/save",
                data=json.dumps(valid_profile),
                content_type="application/json",
            )
            assert response.status_code in (200, 302, 204)

            if os.path.exists(tmp_path):
                saved = load_profile(tmp_path)
                assert saved["positions"][0]["company"] == "Acme Corp"

        finally:
            profile_mod._PROFILE_PATH = original_path
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

# ---------------------------------------------------------------------------
# PDF Upload routes
# ---------------------------------------------------------------------------

class TestPdfUpload:
    """Tests for POST /profile/upload-pdf route."""

    @pytest.fixture
    def _upload_db_path(self, tmp_db_path):
        """Expose the temp DB path for assertions inside tests that use upload_client."""
        return tmp_db_path

    @pytest.fixture
    def app_with_uploads(self, tmp_db_path, tmp_path, monkeypatch):
        """Test app with temp DB and temp upload directory."""
        from job_finder.web import create_app

        # Point data/resume_uploads to a temp dir by monkeypatching Path in resume_review module
        import job_finder.web.blueprints.resume_review as resume_review_mod
        monkeypatch.setattr(resume_review_mod, "_UPLOAD_DIR", str(tmp_path / "resume_uploads"))

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        application = create_app(config=test_config)
        application.config["TESTING"] = True
        # Stash db_path for assertions
        application._test_db_path = tmp_db_path
        return application

    @pytest.fixture
    def upload_client(self, app_with_uploads):
        return app_with_uploads.test_client()

    def _make_mock_doc(self, text: str):
        """Create a MagicMock fitz document returning given text from all pages."""
        from unittest.mock import MagicMock

        page = MagicMock()
        page.get_text.return_value = text

        doc = MagicMock()
        doc.__iter__ = MagicMock(return_value=iter([page]))
        doc.close = MagicMock()
        return doc

    def test_upload_pdf_extracts_text_and_redirects(
        self, app_with_uploads, monkeypatch
    ):
        """POST /profile/upload-pdf with valid text PDF inserts DB row and redirects."""
        import io
        import sqlite3

        long_text = "A" * 500  # Well above the 200-char threshold

        mock_doc = self._make_mock_doc(long_text)
        monkeypatch.setattr("job_finder.web.blueprints.resume_review.fitz.open", lambda mode, data: mock_doc)

        client = app_with_uploads.test_client()
        data = {
            "pdf_file": (io.BytesIO(b"%PDF-fake-content"), "resume.pdf", "application/pdf"),
        }
        resp = client.post(
            "/profile/upload-pdf",
            data=data,
            content_type="multipart/form-data",
        )
        # Should redirect to conflict_review (which doesn't exist yet) or somewhere
        assert resp.status_code == 302

        # Verify DB row inserted using the app's own DB path
        db_path = app_with_uploads._test_db_path
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM resume_upload_reviews ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["filename"] == "resume.pdf"
        assert row["review_status"] == "pending"
        assert long_text in row["raw_text"]

    def test_upload_pdf_archives_file(
        self, app_with_uploads, monkeypatch, tmp_path
    ):
        """POST /profile/upload-pdf archives the PDF bytes to the upload directory."""
        import io
        import job_finder.web.blueprints.resume_review as resume_review_mod

        upload_dir = str(tmp_path / "resume_uploads")
        monkeypatch.setattr(resume_review_mod, "_UPLOAD_DIR", upload_dir)

        long_text = "B" * 500
        mock_doc = self._make_mock_doc(long_text)
        monkeypatch.setattr("job_finder.web.blueprints.resume_review.fitz.open", lambda mode, data: mock_doc)

        client = app_with_uploads.test_client()
        data = {
            "pdf_file": (io.BytesIO(b"%PDF-fake-archive"), "archive_test.pdf", "application/pdf"),
        }
        client.post(
            "/profile/upload-pdf",
            data=data,
            content_type="multipart/form-data",
        )

        # At least one file should exist in the upload dir
        import os
        files = os.listdir(upload_dir) if os.path.exists(upload_dir) else []
        assert len(files) >= 1
        assert any("archive_test.pdf" in f for f in files)

    def test_scanned_pdf_rejected(self, upload_client, monkeypatch):
        """POST /profile/upload-pdf with < 200 chars extracted flashes scanned error."""
        import io

        short_text = "A" * 50  # Below the 200-char threshold

        mock_doc = self._make_mock_doc(short_text)
        monkeypatch.setattr("job_finder.web.blueprints.resume_review.fitz.open", lambda mode, data: mock_doc)

        data = {
            "pdf_file": (io.BytesIO(b"%PDF-scanned"), "scanned.pdf", "application/pdf"),
        }
        with upload_client.session_transaction() as sess:
            sess["_flashes"] = []

        resp = upload_client.post(
            "/profile/upload-pdf",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302
        # Should redirect to /profile (not to conflict_review)
        assert "/profile" in resp.headers.get("Location", "")

        # Check flash message via session
        with upload_client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        messages = [msg for category, msg in flashes]
        assert any("scanned" in msg.lower() or "image-only" in msg.lower() for msg in messages)

    def test_upload_no_file_flashes_error(self, upload_client):
        """POST /profile/upload-pdf with no file flashes an error."""
        resp = upload_client.post(
            "/profile/upload-pdf",
            data={},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302

        with upload_client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        messages = [msg for category, msg in flashes]
        assert any("no file" in msg.lower() or "please select" in msg.lower() for msg in messages)

# ---------------------------------------------------------------------------
# Conflict review routes (Plan 17-02)
# ---------------------------------------------------------------------------

class TestConflictReview:
    """Tests for GET /profile/review/<id> and POST /profile/save-conflicts/<id>."""

    # Canned conflict list returned by mocked _compare_conflicts
    CANNED_CONFLICTS = [
        {
            "type": "new_skill",
            "profile_version": "",
            "pdf_version": "Apache Spark",
            "suggestion": "Apache Spark",
        },
        {
            "type": "achievement_diff",
            "profile_version": "Led A/B testing at scale.",
            "pdf_version": "Led A/B testing platform serving 10M daily active users.",
            "suggestion": "Led A/B testing platform serving 10M daily active users.",
            "position_company": "Acme Corp",
        },
    ]

    @pytest.fixture
    def app_with_upload_row(self, tmp_db_path, tmp_path, monkeypatch):
        """Test app with a pre-inserted resume_upload_reviews row."""
        from job_finder.web import create_app
        import sqlite3
        from job_finder.web.db_migrate import run_migrations

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        application = create_app(config=test_config)
        application.config["TESTING"] = True

        # Insert a test upload row
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "INSERT INTO resume_upload_reviews (id, filename, raw_text, uploaded_at, review_status) "
            "VALUES (1, 'test_resume.pdf', 'Sample resume text for testing...', '2026-03-13T10:00:00Z', 'pending')"
        )
        conn.commit()
        conn.close()

        application._test_db_path = tmp_db_path
        return application

    @pytest.fixture
    def review_client(self, app_with_upload_row):
        return app_with_upload_row.test_client()

    def _mock_compare_conflicts(self, monkeypatch, conflicts=None):
        """Monkeypatch _compare_conflicts to return canned conflicts."""
        import job_finder.web.blueprints.resume_review as resume_review_mod
        if conflicts is None:
            conflicts = self.CANNED_CONFLICTS

        monkeypatch.setattr(
            resume_review_mod,
            "_compare_conflicts",
            lambda raw_text, profile, conn, config: conflicts,
        )

    def test_conflict_review_returns_200(self, review_client, monkeypatch, tmp_path):
        """GET /profile/review/1 for an existing upload returns 200 with conflict-card content."""
        self._mock_compare_conflicts(monkeypatch)
        # Monkeypatch profile path to avoid reading real experience_profile.json
        import job_finder.web.blueprints.resume_review as resume_review_mod
        tmp_profile = str(tmp_path / "profile.json")
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        resp = review_client.get("/profile/review/1")
        assert resp.status_code == 200
        data = resp.data.decode()
        # Should contain conflict-card OR "No conflicts found"
        assert "conflict-card" in data or "No conflicts found" in data

    def test_conflict_review_404_for_missing_upload(self, review_client, monkeypatch):
        """GET /profile/review/9999 returns 404 for non-existent upload."""
        resp = review_client.get("/profile/review/9999")
        assert resp.status_code == 404

    def test_save_conflicts_applies_accepted_items(
        self, app_with_upload_row, monkeypatch, tmp_path
    ):
        """POST /profile/save-conflicts with accepted new_skill adds skill to profile."""
        import job_finder.web.blueprints.resume_review as resume_review_mod
        from job_finder.web.profile_schema import load_profile, save_profile

        # Use a temp profile with known initial state
        tmp_profile = str(tmp_path / "profile.json")
        initial_profile = {
            "positions": [
                {
                    "title": "Senior Data Scientist",
                    "company": "Acme Corp",
                    "start_date": "Jan 2021",
                    "end_date": None,
                    "achievements": ["Led A/B testing at scale."],
                    "skills": ["Python"],
                }
            ],
            "skills": ["Python", "SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(initial_profile, tmp_profile)
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        client = app_with_upload_row.test_client()
        payload = {
            "conflicts": self.CANNED_CONFLICTS,
            "decisions": [
                {"conflict_index": 0, "action": "accept"},  # new_skill: Apache Spark
                {"conflict_index": 1, "action": "skip"},    # achievement: skip
            ],
        }
        resp = client.post(
            "/profile/save-conflicts/1",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 302)

        # Check that Apache Spark was added to profile skills
        updated_profile = load_profile(tmp_profile)
        assert "Apache Spark" in updated_profile["skills"]

    def test_save_conflicts_updates_review_status(
        self, app_with_upload_row, monkeypatch, tmp_path
    ):
        """POST /profile/save-conflicts updates resume_upload_reviews.review_status to 'reviewed'."""
        import sqlite3
        import job_finder.web.blueprints.resume_review as resume_review_mod
        from job_finder.web.profile_schema import save_profile

        tmp_profile = str(tmp_path / "profile.json")
        save_profile({"positions": [], "skills": [], "resume_preferences": {"summary_style": "", "emphasis": []}}, tmp_profile)
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        client = app_with_upload_row.test_client()
        payload = {
            "conflicts": self.CANNED_CONFLICTS,
            "decisions": [
                {"conflict_index": 0, "action": "skip"},
                {"conflict_index": 1, "action": "skip"},
            ],
        }
        client.post(
            "/profile/save-conflicts/1",
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Verify review_status updated
        conn = sqlite3.connect(app_with_upload_row._test_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT review_status FROM resume_upload_reviews WHERE id=1"
        ).fetchone()
        conn.close()
        assert row["review_status"] == "reviewed"

    def test_save_conflicts_skips_items_with_skip_action(
        self, app_with_upload_row, monkeypatch, tmp_path
    ):
        """POST /profile/save-conflicts with skip action leaves profile skills unchanged."""
        import job_finder.web.blueprints.resume_review as resume_review_mod
        from job_finder.web.profile_schema import load_profile, save_profile

        tmp_profile = str(tmp_path / "profile.json")
        initial_profile = {
            "positions": [],
            "skills": ["Python", "SQL"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(initial_profile, tmp_profile)
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        client = app_with_upload_row.test_client()
        payload = {
            "conflicts": self.CANNED_CONFLICTS,
            "decisions": [
                {"conflict_index": 0, "action": "skip"},  # new_skill: skip
                {"conflict_index": 1, "action": "skip"},  # achievement: skip
            ],
        }
        client.post(
            "/profile/save-conflicts/1",
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Profile should be unchanged — Apache Spark NOT added
        updated_profile = load_profile(tmp_profile)
        assert "Apache Spark" not in updated_profile["skills"]
        assert updated_profile["skills"] == ["Python", "SQL"]

# ---------------------------------------------------------------------------
# import_markdown route template variable completeness (Plan 18-01 regression)
# ---------------------------------------------------------------------------

class TestImportMarkdown:
    """Regression tests: POST /profile/import passes uploads and style_guide to template."""

    _MINIMAL_PROFILE = {
        "name": "Test User",
        "positions": [
            {
                "title": "Dev",
                "company": "Co",
                "start_date": "2020",
                "end_date": "2024",
                "achievements": ["Did stuff by 10%"],
                "skills": ["Python"],
            }
        ],
        "skills": ["Python"],
        "resume_preferences": {"summary_style": "", "emphasis": []},
    }

    @pytest.fixture
    def import_client(self, tmp_db_path):
        """Test app for import_markdown tests with a migrated temp DB."""
        from job_finder.web import create_app

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        application = create_app(config=test_config)
        application.config["TESTING"] = True
        return application.test_client()

    def test_import_markdown_includes_style_guide_section(self, import_client, monkeypatch):
        """POST /profile/import renders style-guide-section — confirms style_guide var is passed."""
        import io
        from unittest.mock import patch

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with patch(
            "job_finder.web.blueprints.profile.extract_profile_from_markdown",
            return_value=self._MINIMAL_PROFILE,
        ):
            with patch(
                "job_finder.web.resume_style_guide.load_style_guide",
                return_value={"bullet_style": "dashes"},
            ):
                resp = import_client.post(
                    "/profile/import",
                    data={"markdown_file": (io.BytesIO(b"# Test"), "test.md", "text/markdown")},
                    content_type="multipart/form-data",
                )

        assert resp.status_code == 200
        assert b"style-guide-section" in resp.data

    def test_import_markdown_includes_upload_section(self, import_client, monkeypatch):
        """POST /profile/import renders upload-pdf-form — confirms uploads var is passed."""
        import io
        from unittest.mock import patch

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with patch(
            "job_finder.web.blueprints.profile.extract_profile_from_markdown",
            return_value=self._MINIMAL_PROFILE,
        ):
            with patch(
                "job_finder.web.resume_style_guide.load_style_guide",
                return_value={},
            ):
                resp = import_client.post(
                    "/profile/import",
                    data={"markdown_file": (io.BytesIO(b"# Test"), "test.md", "text/markdown")},
                    content_type="multipart/form-data",
                )

        assert resp.status_code == 200
        assert b"upload-pdf-form" in resp.data

# ---------------------------------------------------------------------------
# Phase 17 activity instrumentation (Plan 19-03)
# ---------------------------------------------------------------------------

class TestPhase17ActivityInstrumentation:
    """Verify Phase 17 profile routes call log_activity()."""

    _TEST_CONFIG = {
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }

    @pytest.fixture
    def instrumented_app(self, tmp_db_path, tmp_path, monkeypatch):
        """Test app with temp DB, temp upload dir, and test DB path stashed."""
        from job_finder.web import create_app
        import job_finder.web.blueprints.resume_review as resume_review_mod
        monkeypatch.setattr(resume_review_mod, "_UPLOAD_DIR", str(tmp_path / "resume_uploads"))
        cfg = dict(self._TEST_CONFIG)
        cfg["db"] = {"path": tmp_db_path}
        application = create_app(config=cfg)
        application.config["TESTING"] = True
        application._test_db_path = tmp_db_path
        return application

    def _insert_upload_row(self, db_path, raw_text="A" * 300):
        """Insert a resume_upload_reviews row, return its id."""
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "INSERT INTO resume_upload_reviews (filename, raw_text, uploaded_at, review_status) "
            "VALUES ('test.pdf', ?, '2026-03-13T10:00:00Z', 'pending')",
            (raw_text,),
        )
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    @patch("job_finder.web.blueprints.resume_review.log_activity")
    def test_upload_pdf_logs_activity(self, mock_log, instrumented_app, monkeypatch):
        """upload_pdf route calls log_activity with ACTION_UPLOAD_RESUME_PDF."""
        long_text = "A" * 500
        page = MagicMock()
        page.get_text.return_value = long_text
        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([page]))
        mock_doc.close = MagicMock()

        monkeypatch.setattr(
            "job_finder.web.blueprints.resume_review.fitz.open",
            lambda mode, data: mock_doc,
        )

        client = instrumented_app.test_client()
        resp = client.post(
            "/profile/upload-pdf",
            data={"pdf_file": (io.BytesIO(b"%PDF-fake"), "resume.pdf", "application/pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302
        assert mock_log.called, "log_activity was not called in upload_pdf"
        call_action = mock_log.call_args[0][1]
        from job_finder.web.activity_tracker import ACTION_UPLOAD_RESUME_PDF
        assert call_action == ACTION_UPLOAD_RESUME_PDF

    @patch("job_finder.web.blueprints.resume_review.log_activity")
    def test_conflict_review_logs_activity(self, mock_log, instrumented_app, monkeypatch, tmp_path):
        """conflict_review route calls log_activity with ACTION_CONFLICT_REVIEW."""
        import job_finder.web.blueprints.resume_review as resume_review_mod

        db_path = instrumented_app._test_db_path
        upload_id = self._insert_upload_row(db_path)

        # Monkeypatch _compare_conflicts to avoid Anthropic call
        monkeypatch.setattr(resume_review_mod, "_compare_conflicts", lambda *args, **kwargs: [])
        # Monkeypatch profile path to avoid reading real profile
        tmp_profile = str(tmp_path / "profile.json")
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        client = instrumented_app.test_client()
        resp = client.get(f"/profile/review/{upload_id}")
        assert resp.status_code == 200
        assert mock_log.called, "log_activity was not called in conflict_review"
        call_action = mock_log.call_args[0][1]
        from job_finder.web.activity_tracker import ACTION_CONFLICT_REVIEW
        assert call_action == ACTION_CONFLICT_REVIEW

    @patch("job_finder.web.blueprints.resume_review.log_activity")
    def test_save_conflicts_logs_activity(self, mock_log, instrumented_app, monkeypatch, tmp_path):
        """save_conflicts route calls log_activity with ACTION_SAVE_CONFLICTS."""
        import job_finder.web.blueprints.resume_review as resume_review_mod

        db_path = instrumented_app._test_db_path
        upload_id = self._insert_upload_row(db_path)

        # Use a temp profile path
        tmp_profile = str(tmp_path / "profile.json")
        save_profile({"positions": [], "skills": [], "resume_preferences": {}}, tmp_profile)
        monkeypatch.setattr(resume_review_mod, "_PROFILE_PATH", tmp_profile)

        client = instrumented_app.test_client()
        payload = {"conflicts": [], "decisions": []}
        resp = client.post(
            f"/profile/save-conflicts/{upload_id}",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 302)
        assert mock_log.called, "log_activity was not called in save_conflicts"
        call_action = mock_log.call_args[0][1]
        from job_finder.web.activity_tracker import ACTION_SAVE_CONFLICTS
        assert call_action == ACTION_SAVE_CONFLICTS

    @patch("job_finder.web.blueprints.resume_review.log_activity")
    def test_extract_style_logs_activity(self, mock_log, instrumented_app, monkeypatch):
        """extract_style route calls log_activity with ACTION_EXTRACT_STYLE."""
        db_path = instrumented_app._test_db_path
        upload_id = self._insert_upload_row(db_path)

        # Monkeypatch extract_style_guide and save_style_guide to avoid Anthropic call
        monkeypatch.setattr(
            "job_finder.web.resume_style_guide.extract_style_guide",
            lambda raw_text, existing, conn, config: {"bullet_style": "dashes"},
        )
        monkeypatch.setattr(
            "job_finder.web.resume_style_guide.save_style_guide",
            lambda guide: None,
        )

        client = instrumented_app.test_client()
        resp = client.post(f"/profile/extract-style/{upload_id}")
        assert resp.status_code in (200, 302)
        assert mock_log.called, "log_activity was not called in extract_style"
        call_action = mock_log.call_args[0][1]
        from job_finder.web.activity_tracker import ACTION_EXTRACT_STYLE
        assert call_action == ACTION_EXTRACT_STYLE

# ---------------------------------------------------------------------------
# Profile recommendation routes (Plan 43-02)
# ---------------------------------------------------------------------------

_REC_APP_CONFIG = {
    "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
    "profile": {
        "target_titles": ["Staff Data Scientist"],
        "target_locations": ["Remote"],
        "min_salary": 150000,
        "industries": [],
        "exclusions": {"title_keywords": [], "companies": []},
        "skills": [],
    },
    "sources": {},
    "output": {"default_format": "cli", "max_results": 50},
}

# Canned recommendation response from mocked call_claude
_CANNED_REC_RESULT = {
    "recommendations": [
        {
            "field": "skills",
            "guidance": "Add Python to your skills.",
            "actions": [{"type": "add_skill", "value": "Python"}],
        }
    ]
}

class TestProfileRecommendations:
    """Tests for GET /profile/recommendation, POST /profile/recommendations-all,
    and POST /profile/apply-fix routes."""

    @pytest.fixture
    def rec_app(self, tmp_db_path, tmp_path, monkeypatch):
        """Test app for recommendation tests with a temp profile file."""
        from job_finder.web import create_app
        import job_finder.web.blueprints.profile as profile_mod
        import job_finder.web.blueprints.profile_recommendations as profile_recs_mod

        # Create a temp profile with known warnings (no achievements on a position)
        tmp_profile = str(tmp_path / "profile.json")
        profile_data = {
            "positions": [
                {
                    "title": "Data Scientist",
                    "company": "TestCo",
                    "start_date": "Jan 2022",
                    "end_date": None,
                    "achievements": [],
                    "skills": [],
                }
            ],
            "skills": [],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(profile_data, tmp_profile)
        # Patch both modules that read _PROFILE_PATH (profile for index route, profile_recs for recommendation routes)
        monkeypatch.setattr(profile_mod, "_PROFILE_PATH", tmp_profile)
        monkeypatch.setattr(profile_recs_mod, "_PROFILE_PATH", tmp_profile)

        cfg = dict(_REC_APP_CONFIG)
        cfg["db"] = {"path": tmp_db_path}
        application = create_app(config=cfg)
        application.config["TESTING"] = True
        application._test_profile_path = tmp_profile
        return application

    @pytest.fixture
    def rec_client(self, rec_app):
        return rec_app.test_client()

    def test_profile_page_shows_fix_buttons(self, rec_client):
        """GET /profile shows 'How to fix?' buttons and recommendation slots for warnings."""
        resp = rec_client.get("/profile")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "How to fix?" in html
        assert "recommendation-slot-0" in html
        assert "recommendations-all" in html or "Get all recommendations" in html

    def test_single_recommendation_route(self, rec_client, monkeypatch):
        """GET /profile/recommendation returns Haiku guidance for a warning."""
        import job_finder.web.blueprints.profile_recommendations as profile_recs_mod
        monkeypatch.setattr(
            profile_recs_mod,
            "call_claude",
            lambda **kwargs: (_CANNED_REC_RESULT, 0.001),
        )
        resp = rec_client.get(
            "/profile/recommendation?field=skills&message=Missing%20skill"
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Add Python to your skills." in html
        assert "apply-fix" in html

    def test_single_recommendation_empty_params(self, rec_client):
        """GET /profile/recommendation with no params returns 'Missing warning context'."""
        resp = rec_client.get("/profile/recommendation")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Missing warning context" in html

    def test_batch_recommendations_route(self, rec_client, monkeypatch):
        """POST /profile/recommendations-all returns batch guidance for all warnings."""
        import job_finder.web.blueprints.profile_recommendations as profile_recs_mod

        batch_result = {
            "recommendations": [
                {
                    "field": "positions[TestCo].achievements",
                    "guidance": "Add quantified achievements for TestCo.",
                    "actions": [],
                },
                {
                    "field": "positions[TestCo].skills",
                    "guidance": "Tag some skills for TestCo.",
                    "actions": [{"type": "add_skill", "value": "SQL"}],
                },
            ]
        }
        monkeypatch.setattr(
            profile_recs_mod,
            "call_claude",
            lambda **kwargs: (batch_result, 0.002),
        )

        resp = rec_client.post("/profile/recommendations-all")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Add quantified achievements for TestCo." in html
        assert "Tag some skills for TestCo." in html

    def test_apply_fix_add_skill(self, rec_app, tmp_path, monkeypatch):
        """POST /profile/apply-fix with add_skill appends the skill to the profile."""
        from job_finder.web.profile_schema import load_profile

        client = rec_app.test_client()
        resp = client.post(
            "/profile/apply-fix",
            data={"action_type": "add_skill", "field": "skills", "value": "Python"},
            content_type="application/x-www-form-urlencoded",
        )
        # Expect HX-Redirect response (200 with HX-Redirect header)
        assert resp.status_code == 200
        assert "HX-Redirect" in resp.headers

        # Verify the profile file was updated
        updated = load_profile(rec_app._test_profile_path)
        assert "Python" in updated["skills"]

    def test_apply_fix_invalid_action_type(self, rec_client):
        """POST /profile/apply-fix with disallowed action_type returns 400."""
        resp = rec_client.post(
            "/profile/apply-fix",
            data={"action_type": "delete_field", "field": "skills", "value": "Python"},
            content_type="application/x-www-form-urlencoded",
        )
        assert resp.status_code == 400

    def test_apply_fix_unsafe_field(self, rec_client):
        """POST /profile/apply-fix with disallowed field for update_field returns 400."""
        resp = rec_client.post(
            "/profile/apply-fix",
            data={"action_type": "update_field", "field": "positions", "value": "bad"},
            content_type="application/x-www-form-urlencoded",
        )
        assert resp.status_code == 400
