"""Tests for Settings blueprint routes.

Covers:
- Config wipe guard: blocks saves that wipe target_titles or skills.
- Normal save: valid form data saves successfully.
- Resume Quality section in settings index page.
- Migrate style guide route.
"""

import os
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def settings_app(tmp_db_path, tmp_path):
    """Create a test Flask app with a temp config file and temp DB."""
    import job_finder.web.blueprints.settings as settings_mod
    from job_finder.web import create_app

    config_path = str(tmp_path / "config.yaml")
    initial_config = {
        "profile": {
            "target_titles": ["Staff Data Scientist", "Senior Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": ["Python", "SQL", "Spark"],
        },
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(initial_config, f, default_flow_style=False)

    # Monkeypatch the config path used by the blueprint
    original_path = settings_mod._CONFIG_PATH
    settings_mod._CONFIG_PATH = config_path

    application = create_app(config=initial_config)
    application.config["TESTING"] = True
    application._test_config_path = config_path

    yield application

    settings_mod._CONFIG_PATH = original_path


@pytest.fixture
def settings_client(settings_app):
    return settings_app.test_client()


class TestSettingsWipeGuard:
    def test_settings_save_blocks_wiping_target_titles(self, settings_client, settings_app):
        """POST /settings/save with empty titles when existing has titles = blocked."""
        resp = settings_client.post(
            "/settings/save",
            data={"target_titles": "", "profile_skills": "Python\nSQL\nSpark"},
        )
        assert resp.status_code == 302

        # Config file should still have titles
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert len(config["profile"]["target_titles"]) == 2

    def test_settings_save_blocks_wiping_skills(self, settings_client, settings_app):
        """POST /settings/save with empty skills when existing has skills = blocked."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert len(config["profile"]["skills"]) == 3

    def test_settings_save_rejects_stale_mtime(self, settings_app):
        """POST /settings/save with stale _config_mtime redirects with warning."""
        import time

        config_path = settings_app._test_config_path
        original_mtime = os.path.getmtime(config_path)

        # Simulate external modification
        time.sleep(0.05)
        with open(config_path, "a", encoding="utf-8") as f:
            f.write("\n# external edit\n")

        client = settings_app.test_client()
        resp = client.post(
            "/settings/save",
            data={
                "_config_mtime": str(original_mtime),
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
            },
        )
        assert resp.status_code == 302

        # Check flash message
        with client.session_transaction() as sess:
            flashes = sess.get("_flashes", [])
        messages = [msg for category, msg in flashes]
        assert any("modified externally" in msg.lower() for msg in messages)

    def test_settings_save_allows_normal_edit(self, settings_client, settings_app):
        """POST /settings/save with valid data = succeeds."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nPrincipal Data Scientist",
                "profile_skills": "Python\nSQL",
                "min_salary": "200000",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert "Principal Data Scientist" in config["profile"]["target_titles"]
        assert config["profile"]["min_salary"] == 200000


def test_settings_index_has_resume_quality_section(settings_app):
    client = settings_app.test_client()
    resp = client.get("/settings/")
    assert resp.status_code == 200
    assert b"Resume Quality" in resp.data
    assert b"Migrate Style Guide" in resp.data
    assert b"style-guide-migrate-section" in resp.data
    assert b"guideline fields populated" in resp.data
    assert b"available for migration" in resp.data


def test_settings_migrate_shows_spinner_indicator(settings_app):
    """Settings page migrate button has HTMX loading indicator markup."""
    client = settings_app.test_client()
    resp = client.get("/settings/")
    assert resp.status_code == 200
    assert b"htmx-indicator" in resp.data
    assert b"migrate-spinner" in resp.data
    assert b"hx-disabled-elt" in resp.data


def test_migrate_style_guide_route_returns_200(settings_app):
    client = settings_app.test_client()
    with patch("job_finder.web.blueprints.guidelines.migrate_style_guide") as mock_migrate:
        mock_migrate.return_value = {"bullet_style": "dashes", "summary_formula": "test"}
        resp = client.post("/settings/migrate-style-guide")
    assert resp.status_code == 200


class TestGuidelinesImport:
    """Tests for the Update Guidelines section: preview and apply routes."""

    def test_settings_page_shows_guidelines_textarea(self, settings_app):
        """GET /settings returns 200 with guidelines textarea and preview button."""
        client = settings_app.test_client()
        resp = client.get("/settings/")
        assert resp.status_code == 200
        assert b'id="guidelines-textarea"' in resp.data
        assert b"preview-guidelines-merge" in resp.data

    def test_preview_empty_text_returns_error(self, settings_app):
        """POST /settings/preview-guidelines-merge with empty text returns error fragment."""
        client = settings_app.test_client()
        resp = client.post(
            "/settings/preview-guidelines-merge",
            data={"guidelines_text": ""},
        )
        assert resp.status_code == 200
        assert b"Please enter guidelines text" in resp.data

    def test_preview_returns_diff_html(self, settings_app):
        """POST /settings/preview-guidelines-merge returns diff HTML with stashed JSON."""
        merged_result = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Experience"],
            "tone": "professional",
            "date_format": "MMM YYYY",
        }
        client = settings_app.test_client()
        with patch(
            "job_finder.web.blueprints.guidelines.merge_guidelines_into_guide"
        ) as mock_merge:
            mock_merge.return_value = merged_result
            resp = client.post(
                "/settings/preview-guidelines-merge",
                data={"guidelines_text": "Use dashes for bullets"},
            )
        assert resp.status_code == 200
        assert b"merged_guide_json" in resp.data
        assert b"guidelines-diff-container" in resp.data
        assert b"apply-guidelines-merge" in resp.data

    def test_apply_saves_merged_guide(self, settings_app):
        """POST /settings/apply-guidelines-merge saves the stashed dict and returns success."""
        import json as json_mod

        merged_guide = {
            "bullet_style": "dashes",
            "verb_tense": "past",
            "section_order": ["Experience"],
            "tone": "professional",
            "date_format": "MMM YYYY",
        }
        client = settings_app.test_client()
        with patch("job_finder.web.blueprints.guidelines.save_style_guide") as mock_save:
            resp = client.post(
                "/settings/apply-guidelines-merge",
                data={"merged_guide_json": json_mod.dumps(merged_guide)},
            )
        assert resp.status_code == 200
        assert b"applied successfully" in resp.data
        mock_save.assert_called_once_with(merged_guide)
