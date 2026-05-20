"""Tests for Settings blueprint routes.

Covers:
- Config wipe guard: blocks saves that wipe target_titles or skills.
- Normal save: valid form data saves successfully.
"""

import os

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


class TestSettingsKeyringWrite:
    """Commit 3.5: SerpAPI / JSearch keys submitted via the Settings form
    land in the OS keyring; the plaintext field in config.yaml is cleared on
    success; empty submissions leave existing secrets alone (no clobber)."""

    def test_save_serpapi_key_writes_to_keyring(self, settings_client, settings_app):
        """Non-empty serpapi_api_key submission → keyring set, plaintext cleared."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "serpapi_api_key": "sk-test-from-form-abc",
            },
        )
        assert resp.status_code == 302

        assert (
            keyring.get_password("job-cannon", "sources.serpapi.api_key")
            == "sk-test-from-form-abc"
        )

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        api_key = saved.get("sources", {}).get("serpapi", {}).get("api_key", "<missing>")
        assert api_key == "", f"plaintext should be cleared, got {api_key!r}"

    def test_save_empty_serpapi_key_does_not_clobber_existing(
        self, settings_client, settings_app
    ):
        """Empty serpapi_api_key (user didn't type) → existing config untouched.

        With the (set)/(not set) placeholder UX, an empty submission means
        "leave the secret alone". The save path must not include api_key="" in
        the form_config, which would otherwise deep-merge-overwrite a real
        existing plaintext value (or signal a clear when keyring is empty).
        """
        # Plant an existing plaintext value via direct config write.
        config_path = settings_app._test_config_path
        with open(config_path, encoding="utf-8") as f:
            existing = yaml.safe_load(f)
        existing.setdefault("sources", {})["serpapi"] = {
            "enabled": True,
            "api_key": "sk-already-there",
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False)

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "serpapi_api_key": "",  # empty — user did not type
            },
        )
        assert resp.status_code == 302

        with open(config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert (
            saved["sources"]["serpapi"]["api_key"] == "sk-already-there"
        ), "empty submission should not have wiped existing plaintext"

    def test_index_passes_secret_set_with_keyring_value(self, settings_client):
        """GET /settings → template receives secret_set reflecting keyring state."""
        import keyring

        keyring.set_password("job-cannon", "sources.serpapi.api_key", "sk-planted")
        try:
            resp = settings_client.get("/settings/")
            assert resp.status_code == 200
            body = resp.get_data(as_text=True)
            # SerpAPI input now renders the "set" placeholder.
            assert "(set — type to replace)" in body
            # And does NOT leak any plaintext into the value attribute.
            assert 'name="serpapi_api_key"' in body
            assert "sk-planted" not in body
        finally:
            keyring.delete_password("job-cannon", "sources.serpapi.api_key")

    def test_index_passes_secret_set_when_not_set(self, settings_client):
        """GET /settings with no keyring entry → '(not set)' placeholder."""
        resp = settings_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "(not set)" in body


def test_parse_form_does_not_write_scoring_models():
    """Regression: 2026-05-21 TIER-RENAME-ECHO dropped scoring.models from
    config. A representative form payload with the now-deleted model_low /
    model_mid fields must not produce a `scoring.models` key in the merged
    config — those inputs were write-only to a dead schema.
    """
    from werkzeug.datastructures import ImmutableMultiDict

    from job_finder.web.blueprints.settings import _parse_form_to_config

    # Even if a malicious or stale form re-introduces these field names,
    # the parser should ignore them.
    form = ImmutableMultiDict(
        [
            ("target_titles", "Engineer"),
            ("model_low", "claude-haiku-4-5"),
            ("model_mid", "claude-sonnet-4-6"),
        ]
    )
    result = _parse_form_to_config(form)
    scoring = result.get("scoring", {})
    assert "models" not in scoring, (
        f"scoring.models should be dropped, got {scoring!r}"
    )
