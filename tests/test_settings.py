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

    def test_save_empty_serpapi_key_does_not_clobber_existing(self, settings_client, settings_app):
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
        assert saved["sources"]["serpapi"]["api_key"] == "sk-already-there", (
            "empty submission should not have wiped existing plaintext"
        )

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
    assert "models" not in scoring, f"scoring.models should be dropped, got {scoring!r}"


class TestSettingsStage6AdvancedSection:
    """Stage 6 (2026-05-22): paid SERP providers grouped under <details
    summary="Advanced: paid SERP providers (optional)"> collapsed by
    default. Verifies the wrapper element exists with the right summary
    text, is collapsed (no `open` attribute), and all four tiles' form
    fields are present inside the body."""

    def test_advanced_section_present_and_collapsed(self, settings_client):
        resp = settings_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        # The <details> wrapper exists with the Q5-default summary text.
        assert "<details" in body
        assert "Advanced: paid SERP providers (optional)" in body

        # Collapsed by default: the rendered <details> tag must not have
        # the `open` attribute. (jinja2 does not insert one.)
        # Find the specific details element by its summary text.
        idx = body.find("Advanced: paid SERP providers")
        # Walk back to the opening <details tag.
        details_start = body.rfind("<details", 0, idx)
        assert details_start >= 0, "could not locate <details before summary"
        details_open_tag = body[details_start : body.find(">", details_start) + 1]
        assert " open" not in details_open_tag, (
            f"details should be collapsed by default; got tag {details_open_tag!r}"
        )

    def test_all_four_tile_fields_present(self, settings_client):
        """All four tiles render their form field names — proving they
        actually live inside the rendered HTML and survive the wrap."""
        resp = settings_client.get("/settings/")
        body = resp.get_data(as_text=True)

        # SerpAPI tile (pre-existing)
        assert 'name="serpapi_enabled"' in body
        assert 'name="serpapi_api_key"' in body
        # Thordata tile (pre-existing)
        assert 'name="thordata_enabled"' in body
        assert 'name="thordata_api_key"' in body
        assert 'name="thordata_max_age_days"' in body
        # DataForSEO tile (Stage 6 NEW)
        assert 'name="dataforseo_enabled"' in body
        assert 'name="dataforseo_api_key"' in body
        assert 'name="dataforseo_depth"' in body
        assert 'name="dataforseo_priority"' in body
        assert 'name="dataforseo_max_age_days"' in body
        # Google CSE tile (Stage 6 NEW)
        assert 'name="google_cse_enabled"' in body
        assert 'name="google_cse_api_key"' in body
        assert 'name="google_cse_cse_id"' in body

    def test_explanatory_note_present(self, settings_client):
        """The Q5 default note listing the free alternatives must render."""
        resp = settings_client.get("/settings/")
        body = resp.get_data(as_text=True)
        # The note enumerates the free alternatives users get out of the box.
        assert "RemoteOK" in body
        assert "Y Combinator" in body or "Work at a Startup" in body
        assert "Google Programmable Search" in body

    def test_ats_blurb_lists_all_12_platforms(self, settings_client):
        """The ATS Scanning section's intro blurb must enumerate all 12
        currently-supported platforms, not just Lever/Greenhouse/Ashby
        as it did pre-Stage-6."""
        resp = settings_client.get("/settings/")
        body = resp.get_data(as_text=True)
        for platform in (
            "Lever",
            "Greenhouse",
            "Ashby",
            "Workday",
            "SmartRecruiters",
            "Recruitee",
            "Breezy",
            "JazzHR",
            "Pinpoint",
            "Personio",
            "BambooHR",
            "Teamtailor",
        ):
            assert platform in body, f"{platform} missing from settings page"


class TestSettingsDataForSEOPersistence:
    """Stage 6 (2026-05-22): the new DataForSEO tile actually saves through
    to config.yaml + keyring on POST /settings/save."""

    def test_save_dataforseo_settings_persists(self, settings_client, settings_app):
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "dataforseo_enabled": "on",
                "dataforseo_max_age_days": "14",
                "dataforseo_depth": "100",
                "dataforseo_priority": "2",
                "_dataforseo_queries_present": "1",
                "dataforseo_query_0": "Staff ML Engineer",
                "dataforseo_location_0": "Remote",
                "dataforseo_query_1": "Principal Data Scientist",
                "dataforseo_location_1": "San Francisco",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)

        dfs = saved["sources"]["dataforseo"]
        assert dfs["enabled"] is True
        assert dfs["max_age_days"] == 14
        assert dfs["depth"] == 100
        assert dfs["priority"] == 2
        assert dfs["queries"] == [
            {"query": "Staff ML Engineer", "location": "Remote"},
            {"query": "Principal Data Scientist", "location": "San Francisco"},
        ]

    def test_save_dataforseo_api_key_writes_to_keyring(self, settings_client, settings_app):
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "dataforseo_api_key": "base64-encoded-creds-xyz",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.dataforseo.api_key")
                == "base64-encoded-creds-xyz"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            api_key = saved.get("sources", {}).get("dataforseo", {}).get("api_key", "<missing>")
            assert api_key == ""
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.dataforseo.api_key")
            except Exception:
                pass

    def test_save_dataforseo_depth_clamped_to_valid_range(self, settings_client, settings_app):
        """Depth must be in [10, 200] per DataForSEO docs. Out-of-range
        submissions get clamped, not propagated as-is."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "dataforseo_depth": "9999",  # out of range
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["dataforseo"]["depth"] == 200

    def test_save_dataforseo_priority_falls_back_to_normal(self, settings_client, settings_app):
        """Priority outside {1, 2} falls back to 1 (normal)."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "dataforseo_priority": "99",  # invalid
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["dataforseo"]["priority"] == 1


class TestSettingsGoogleCSEPersistence:
    """Stage 6 (2026-05-22): the new Google CSE tile saves api_key + cse_id
    through to the keyring, leaving the config.yaml plaintext cleared."""

    def test_save_google_cse_settings_persists(self, settings_client, settings_app):
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "google_cse_enabled": "on",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["google_cse"]["enabled"] is True

    def test_save_google_cse_api_key_and_cse_id_write_to_keyring(
        self, settings_client, settings_app
    ):
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "google_cse_api_key": "AIzaTEST-google-api-key",
                "google_cse_cse_id": "0123456789abcdef:cse-id",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.google_cse.api_key")
                == "AIzaTEST-google-api-key"
            )
            assert (
                keyring.get_password("job-cannon", "sources.google_cse.cse_id")
                == "0123456789abcdef:cse-id"
            )
            # Both plaintext leaf values cleared in config.yaml after the
            # keyring write (per _move_secret_to_keyring contract).
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            cse_block = saved.get("sources", {}).get("google_cse", {})
            assert cse_block.get("api_key", "<missing>") == ""
            assert cse_block.get("cse_id", "<missing>") == ""
        finally:
            for canonical in ("sources.google_cse.api_key", "sources.google_cse.cse_id"):
                try:
                    keyring.delete_password("job-cannon", canonical)
                except keyring.errors.PasswordDeleteError:
                    pass

    def test_save_empty_google_cse_credentials_does_not_clobber_existing(
        self, settings_client, settings_app
    ):
        """Empty CSE inputs (user didn't type) leave existing values alone."""
        config_path = settings_app._test_config_path
        with open(config_path, encoding="utf-8") as f:
            existing = yaml.safe_load(f)
        existing.setdefault("sources", {})["google_cse"] = {
            "enabled": True,
            "api_key": "AIza-prior",
            "cse_id": "cse-prior",
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False)

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "google_cse_api_key": "",
                "google_cse_cse_id": "",
            },
        )
        assert resp.status_code == 302
        with open(config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        # The deep-merge preserved the existing plaintext values because the
        # parser dropped the empty leaves from form_config.
        assert saved["sources"]["google_cse"]["api_key"] == "AIza-prior"
        assert saved["sources"]["google_cse"]["cse_id"] == "cse-prior"


class TestSettingsThordataPersistence:
    """Stage 6 (2026-05-22): the pre-existing Thordata tile was rendered
    but never persisted — `_parse_form_to_config` had no Thordata block.
    These tests pin the now-working behavior down so we can't regress."""

    def test_save_thordata_enabled_and_max_age_persists(self, settings_client, settings_app):
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "thordata_enabled": "on",
                "thordata_max_age_days": "10",
                "_thordata_queries_present": "1",
                "thordata_query_0": "Senior ML",
                "thordata_location_0": "Remote",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        td = saved["sources"]["thordata"]
        assert td["enabled"] is True
        assert td["max_age_days"] == 10
        assert td["queries"] == [{"query": "Senior ML", "location": "Remote"}]

    def test_save_thordata_api_key_writes_to_keyring(self, settings_client, settings_app):
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "thordata_api_key": "td-test-key",
            },
        )
        assert resp.status_code == 302
        try:
            assert keyring.get_password("job-cannon", "sources.thordata.api_key") == "td-test-key"
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            api_key = saved.get("sources", {}).get("thordata", {}).get("api_key", "<missing>")
            assert api_key == ""
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.thordata.api_key")
            except Exception:
                pass
