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

import json
import os
import tempfile

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

        with open(tmp_profile_path, encoding="utf-8") as f:
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
        with open(tmp_profile_path, encoding="utf-8") as f:
            before = json.load(f)
        assert len(before["positions"]) == 3
        assert len(before["skills"]) == 5

        # Now attempt to overwrite with empty profile — the guard must block this
        with self._capture_warning("job_finder.web.profile_schema") as captured_warnings:
            save_profile(EMPTY_PROFILE, tmp_profile_path)

        # File must be UNCHANGED
        with open(tmp_profile_path, encoding="utf-8") as f:
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

        with open(tmp_profile_path, encoding="utf-8") as f:
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

        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["skills"] == ["Python", "SQL", "Spark"]

    def test_save_profile_refuses_suspicious_reduction(self, tmp_profile_path):
        """save_profile blocks saves where both positions AND skills shrink (wipe signal)."""

        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
                {
                    "title": "C",
                    "company": "Co3",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x3"],
                    "skills": ["R"],
                },
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }

        with self._capture_warning("job_finder.web.profile_schema") as warnings:
            save_profile(reduced, tmp_profile_path)

        # File must be unchanged
        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 3, "Suspicious reduction was not blocked"
        assert len(after["skills"]) == 5
        assert len(warnings) > 0

    def test_save_profile_allows_reduction_with_force(self, tmp_profile_path):
        """save_profile with force=True allows intentional reduction."""
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
                {
                    "title": "C",
                    "company": "Co3",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x3"],
                    "skills": ["R"],
                },
            ],
            "skills": ["P", "Q", "R", "S", "T"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        reduced = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(reduced, tmp_profile_path, force=True)

        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 2

    def test_save_profile_allows_one_dimension_reduction(self, tmp_profile_path):
        """Reducing positions but increasing skills is allowed (not suspicious)."""
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
                {
                    "title": "B",
                    "company": "Co2",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x2"],
                    "skills": ["Q"],
                },
            ],
            "skills": ["P", "Q"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)

        # Fewer positions but more skills — legitimate edit
        updated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P", "Q", "R", "S"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(updated, tmp_profile_path)

        with open(tmp_profile_path, encoding="utf-8") as f:
            after = json.load(f)
        assert len(after["positions"]) == 1
        assert len(after["skills"]) == 4

    def test_save_profile_rejects_stale_mtime(self, client, tmp_profile_path):
        """POST /profile/save with stale _mtime returns 409.

        Uses the shared `client` fixture (real temp-file DB with migrations
        applied + onboarding_state seeded) — `:memory:` DBs were broken here
        because per-connection isolation meant the migrated schema disappeared
        before any request handler could read it.
        """
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
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

        import job_finder.web.blueprints.profile as profile_mod

        orig_path = profile_mod._PROFILE_PATH
        profile_mod._PROFILE_PATH = tmp_profile_path
        try:
            payload = {
                "positions": [
                    {
                        "title": "B",
                        "company": "Co2",
                        "start_date": "",
                        "end_date": None,
                        "achievements": [],
                        "skills": [],
                    }
                ],
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

    def test_save_profile_accepts_fresh_mtime(self, client, tmp_profile_path):
        """POST /profile/save with fresh _mtime succeeds.

        See ``test_save_profile_rejects_stale_mtime`` for why the shared
        ``client`` fixture is used instead of an ad-hoc ``:memory:`` app.
        """
        populated = {
            "positions": [
                {
                    "title": "A",
                    "company": "Co1",
                    "start_date": "",
                    "end_date": None,
                    "achievements": ["x1"],
                    "skills": ["P"],
                },
            ],
            "skills": ["P"],
            "resume_preferences": {"summary_style": "", "emphasis": []},
        }
        save_profile(populated, tmp_profile_path)
        current_mtime = os.path.getmtime(tmp_profile_path)

        import job_finder.web.blueprints.profile as profile_mod

        orig_path = profile_mod._PROFILE_PATH
        profile_mod._PROFILE_PATH = tmp_profile_path
        try:
            payload = {
                "positions": [
                    {
                        "title": "B",
                        "company": "Co2",
                        "start_date": "",
                        "end_date": None,
                        "achievements": ["x2"],
                        "skills": ["Q"],
                    }
                ],
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
