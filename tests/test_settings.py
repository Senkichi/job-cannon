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


class TestSettingsStage7FreePortalsTile:
    """Stage 7 (2026-05-22): Settings UI tiles for the free job portals.

    The portal_search master + Jobicy/YC/USAJobs/Adzuna/Jooble sub-portals
    + JSearch are now editable from the Settings page. Credentials for
    USAJobs/Adzuna/Jooble are persisted to config.yaml plaintext (under
    0600 perms) — the keyring path is NOT wired for these because the
    secrets.py canonical names use a top-level layout while the read
    site at job_finder.sources.portal_search_source expects nested
    portal_search.<name>.* — addressing that mismatch is out of Stage 7
    scope. JSearch keeps its existing keyring routing.
    """

    def test_index_renders_with_portal_tile(self, settings_client):
        resp = settings_client.get("/settings")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Master switch
        assert "portal_search_enabled" in body
        assert "Job Portals (free)" in body
        # Sub-portals
        assert "portal_search_jobicy_enabled" in body
        assert "portal_search_yc_enabled" in body
        assert "portal_search_usajobs_enabled" in body
        assert "portal_search_adzuna_enabled" in body
        assert "portal_search_jooble_enabled" in body
        # JSearch tile (new — was wired in parser but had no UI)
        assert "jsearch_enabled" in body
        assert "jsearch_rapidapi_key" in body
        # Stage 7.4 — keywords-fallback hint copy (Finding #3). Production
        # _fetch_portal_search falls back to profile.target_titles when
        # keywords is empty; this hint surfaces that contract in the UI.
        assert "Leave empty to use your target titles." in body

    def test_save_portal_search_master_persists(self, settings_client, settings_app):
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_enabled": "on",
                "portal_search_keywords": "Staff Engineer\nML Platform",
                "portal_search_max_serp_queries": "20",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        ps = saved["sources"]["portal_search"]
        assert ps["enabled"] is True
        assert ps["keywords"] == ["Staff Engineer", "ML Platform"]
        assert ps["max_serp_queries"] == 20

    def test_save_keyless_subportals_persists(self, settings_client, settings_app):
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_jobicy_enabled": "on",
                "portal_search_yc_enabled": "on",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        ps = saved["sources"]["portal_search"]
        assert ps["jobicy"]["enabled"] is True
        assert ps["yc_workatastartup"]["enabled"] is True

    def test_save_usajobs_credentials_persists_keyring(self, settings_client, settings_app):
        """Stage 7.1: USAJobs creds route to OS keyring; plaintext is wiped."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_usajobs_enabled": "on",
                "portal_search_usajobs_user_agent_email": "me@example.com",
                "portal_search_usajobs_authorization_key": "usajobs-test-key",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password(
                    "job-cannon", "sources.portal_search.usajobs.user_agent_email"
                )
                == "me@example.com"
            )
            assert (
                keyring.get_password(
                    "job-cannon", "sources.portal_search.usajobs.authorization_key"
                )
                == "usajobs-test-key"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            usajobs = saved["sources"]["portal_search"]["usajobs"]
            # enabled toggle still in config; cred fields wiped after keyring write.
            assert usajobs["enabled"] is True
            assert usajobs.get("user_agent_email", "") == ""
            assert usajobs.get("authorization_key", "") == ""
        finally:
            for name in (
                "sources.portal_search.usajobs.user_agent_email",
                "sources.portal_search.usajobs.authorization_key",
            ):
                try:
                    keyring.delete_password("job-cannon", name)
                except Exception:
                    pass

    def test_save_adzuna_credentials_persists_keyring(self, settings_client, settings_app):
        """Stage 7.1: Adzuna creds route to OS keyring; country stays plaintext."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_adzuna_enabled": "on",
                "portal_search_adzuna_app_id": "adzuna-id",
                "portal_search_adzuna_app_key": "adzuna-key",
                "portal_search_adzuna_country": "gb",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.adzuna.app_id")
                == "adzuna-id"
            )
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.adzuna.app_key")
                == "adzuna-key"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            adzuna = saved["sources"]["portal_search"]["adzuna"]
            assert adzuna["enabled"] is True
            assert adzuna.get("app_id", "") == ""
            assert adzuna.get("app_key", "") == ""
            # Country is not a secret; stays plaintext.
            assert adzuna["country"] == "gb"
        finally:
            for name in (
                "sources.portal_search.adzuna.app_id",
                "sources.portal_search.adzuna.app_key",
            ):
                try:
                    keyring.delete_password("job-cannon", name)
                except Exception:
                    pass

    def test_save_jooble_credentials_persists_keyring(self, settings_client, settings_app):
        """Stage 7.1: Jooble api_key routes to OS keyring; plaintext is wiped."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_jooble_enabled": "on",
                "portal_search_jooble_api_key": "jooble-test-key",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.jooble.api_key")
                == "jooble-test-key"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            jooble = saved["sources"]["portal_search"]["jooble"]
            assert jooble["enabled"] is True
            assert jooble.get("api_key", "") == ""
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.portal_search.jooble.api_key")
            except Exception:
                pass

    def test_save_empty_credentials_preserves_keyring(self, settings_client):
        """No-op save (toggles on, cred fields empty) must not clobber existing
        keyring entries. Stage 7.1: parser's `and form[x]` guard prevents the
        empty leaf from entering form_config at all, so _move_secret_to_keyring
        finds nothing to migrate and the existing keyring entries are untouched.
        """
        import keyring

        # Seed keyring directly (mimics a prior save() with creds populated)
        keyring.set_password(
            "job-cannon", "sources.portal_search.usajobs.user_agent_email", "old@x.com"
        )
        keyring.set_password(
            "job-cannon", "sources.portal_search.usajobs.authorization_key", "OLD"
        )
        keyring.set_password("job-cannon", "sources.portal_search.adzuna.app_id", "OLD_ID")
        keyring.set_password("job-cannon", "sources.portal_search.adzuna.app_key", "OLD_KEY")
        keyring.set_password("job-cannon", "sources.portal_search.jooble.api_key", "OLD_JK")

        try:
            # Submit form with toggles re-enabled but blank credential fields
            # (this is what happens when the user just edits something else).
            resp = settings_client.post(
                "/settings/save",
                data={
                    "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                    "profile_skills": "Python\nSQL\nSpark",
                    "portal_search_enabled": "on",
                    "portal_search_usajobs_enabled": "on",
                    "portal_search_usajobs_user_agent_email": "",
                    "portal_search_usajobs_authorization_key": "",
                    "portal_search_adzuna_enabled": "on",
                    "portal_search_adzuna_app_id": "",
                    "portal_search_adzuna_app_key": "",
                    "portal_search_adzuna_country": "us",
                    "portal_search_jooble_enabled": "on",
                    "portal_search_jooble_api_key": "",
                },
            )
            assert resp.status_code == 302
            # Keyring entries preserved.
            assert (
                keyring.get_password(
                    "job-cannon", "sources.portal_search.usajobs.user_agent_email"
                )
                == "old@x.com"
            )
            assert (
                keyring.get_password(
                    "job-cannon", "sources.portal_search.usajobs.authorization_key"
                )
                == "OLD"
            )
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.adzuna.app_id")
                == "OLD_ID"
            )
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.adzuna.app_key")
                == "OLD_KEY"
            )
            assert (
                keyring.get_password("job-cannon", "sources.portal_search.jooble.api_key")
                == "OLD_JK"
            )
        finally:
            for name in (
                "sources.portal_search.usajobs.user_agent_email",
                "sources.portal_search.usajobs.authorization_key",
                "sources.portal_search.adzuna.app_id",
                "sources.portal_search.adzuna.app_key",
                "sources.portal_search.jooble.api_key",
            ):
                try:
                    keyring.delete_password("job-cannon", name)
                except Exception:
                    pass

    def test_save_jsearch_settings_persists_keyring(self, settings_client, settings_app):
        """JSearch already routed through keyring in save(); the new tile feeds it."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "jsearch_enabled": "on",
                "jsearch_rapidapi_key": "jsearch-test-key",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.jsearch.rapidapi_key")
                == "jsearch-test-key"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            assert saved["sources"]["jsearch"]["enabled"] is True
            # Keyring captured the secret; config.yaml plaintext is wiped.
            assert saved["sources"]["jsearch"].get("rapidapi_key", "") == ""
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.jsearch.rapidapi_key")
            except Exception:
                pass

    def test_adzuna_country_lowercased(self, settings_client, settings_app):
        """ISO country codes are case-insensitive on input — normalize to lowercase."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "portal_search_adzuna_enabled": "on",
                "portal_search_adzuna_country": "GB",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["portal_search"]["adzuna"]["country"] == "gb"


class TestSettingsStage72ImapTile:
    """Stage 7.2 (2026-05-23): Settings-side IMAP credentials tile.

    Previously IMAP creds could only be set via the onboarding wizard. The
    Settings tile is the edit/re-issue path — same canonical keyring name
    as onboarding (sources.imap.app_password), so values written from
    either page resolve via the same precedence stack at read time.
    """

    def test_index_renders_imap_tile(self, settings_client):
        resp = settings_client.get("/settings/")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Gmail (IMAP)" in body
        assert "imap_enabled" in body
        assert "imap_email" in body
        assert "imap_app_password" in body
        assert "imap_host" in body
        assert "imap_port" in body
        assert "imap_folder" in body

    def test_save_imap_credentials_persists_keyring(self, settings_client, settings_app):
        """app_password routes to OS keyring; other fields stay plaintext."""
        import keyring

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "imap_enabled": "on",
                "imap_email": "me@gmail.com",
                "imap_app_password": "abcd efgh ijkl mnop",
                "imap_host": "imap.gmail.com",
                "imap_port": "993",
                "imap_folder": "INBOX",
            },
        )
        assert resp.status_code == 302
        try:
            assert (
                keyring.get_password("job-cannon", "sources.imap.app_password")
                == "abcd efgh ijkl mnop"
            )
            with open(settings_app._test_config_path, encoding="utf-8") as f:
                saved = yaml.safe_load(f)
            imap = saved["sources"]["imap"]
            assert imap["enabled"] is True
            assert imap["email"] == "me@gmail.com"
            # Keyring captured the secret; config.yaml plaintext wiped.
            assert imap.get("app_password", "") == ""
            assert imap["host"] == "imap.gmail.com"
            assert imap["port"] == 993
            assert imap["folder"] == "INBOX"
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.imap.app_password")
            except Exception:
                pass

    def test_save_empty_app_password_preserves_keyring(self, settings_client):
        """Empty app_password field on re-save preserves the existing keyring entry.

        This is the common case: user edits some other field and the password
        input renders empty by design (we never round-trip the secret to HTML).
        """
        import keyring

        keyring.set_password("job-cannon", "sources.imap.app_password", "PRESERVED")
        try:
            resp = settings_client.post(
                "/settings/save",
                data={
                    "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                    "profile_skills": "Python\nSQL\nSpark",
                    "imap_enabled": "on",
                    "imap_email": "me@gmail.com",
                    "imap_app_password": "",
                    "imap_host": "imap.gmail.com",
                    "imap_port": "993",
                    "imap_folder": "INBOX",
                },
            )
            assert resp.status_code == 302
            assert keyring.get_password("job-cannon", "sources.imap.app_password") == "PRESERVED"
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.imap.app_password")
            except Exception:
                pass

    def test_save_preserves_spaces_in_app_password(self, settings_client):
        """App passwords from Google may include spaces; never strip()."""
        import keyring

        # Includes leading + trailing + middle spaces — must survive verbatim.
        raw = " abcd efgh ijkl mnop "
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist",
                "profile_skills": "Python",
                "imap_enabled": "on",
                "imap_email": "me@gmail.com",
                "imap_app_password": raw,
            },
        )
        assert resp.status_code == 302
        try:
            assert keyring.get_password("job-cannon", "sources.imap.app_password") == raw
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.imap.app_password")
            except Exception:
                pass

    def test_save_defaults_filled_when_host_port_folder_blank(self, settings_client, settings_app):
        """Blank host/folder fall back to imap.gmail.com / INBOX; port defaults to 993."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist",
                "profile_skills": "Python",
                "imap_enabled": "on",
                "imap_email": "me@gmail.com",
                # leave password blank — we only care about defaults here
                "imap_app_password": "",
                # blank host/folder, port omitted entirely
                "imap_host": "",
                "imap_folder": "",
            },
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        imap = saved["sources"]["imap"]
        assert imap["email"] == "me@gmail.com"
        # Blank host string skipped (defaults preserved via ingestion-side fallback).
        assert "host" not in imap or imap["host"] == "imap.gmail.com"
        assert "folder" not in imap or imap["folder"] == "INBOX"

    def test_secret_set_flag_drives_placeholder(self, settings_client):
        """secret_set['sources.imap.app_password'] flips True after a keyring write,
        which is what swaps the password input's placeholder from (not set) to (set)."""
        import re

        import keyring

        # Initially not set — placeholder should be "(not set)"
        try:
            keyring.delete_password("job-cannon", "sources.imap.app_password")
        except Exception:
            pass
        resp = settings_client.get("/settings/")
        body = resp.data.decode("utf-8")
        # Scope the assertion to the imap_app_password input block.
        m = re.search(r'name="imap_app_password"[^>]*placeholder="([^"]+)"', body)
        assert m, "imap_app_password input missing"
        assert m.group(1) == "(not set)"

        # Set + verify placeholder flip to (set ...)
        keyring.set_password("job-cannon", "sources.imap.app_password", "x")
        try:
            resp2 = settings_client.get("/settings/")
            body2 = resp2.data.decode("utf-8")
            m2 = re.search(r'name="imap_app_password"[^>]*placeholder="([^"]+)"', body2)
            assert m2
            assert "set" in m2.group(1).lower()
            assert "type to replace" in m2.group(1).lower()
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.imap.app_password")
            except Exception:
                pass


class TestSettingsCheckboxBrowserShape:
    """Discovery #4 (2026-05-22 Stage 7.3 shakedown): the settings form
    template emits a hidden empty input AND a real checkbox under the same
    name, so unchecked boxes still post the field. Werkzeug ``form[name]``
    returns the FIRST matching value — the hidden's empty string — which
    made the legacy ``form[name] == "on"`` pattern always evaluate False
    even when the box was checked.

    Browser-driven E2E surfaced the bug; existing Stage 1–7 tests missed it
    because they only POST ``{name: "on"}`` without the hidden pair.

    These regression tests POST the actual browser shape (list of tuples
    with duplicate keys) for every checkbox in the form and confirm True
    persists. Fix lives in ``settings.py::_checked``.
    """

    def _browser_post(self, settings_client, fields):
        """POST fields with duplicate-key form encoding.

        ``fields`` is a list of (name, value) tuples. Always includes the
        profile required-fields so the save handler doesn't reject. Wraps
        in a MultiDict so Werkzeug preserves duplicate keys.
        """
        from werkzeug.datastructures import MultiDict

        base = [
            ("target_titles", "Staff Data Scientist\nSenior Data Scientist"),
            ("profile_skills", "Python\nSQL\nSpark"),
        ]
        return settings_client.post("/settings/save", data=MultiDict(base + list(fields)))

    def test_gmail_enabled_persists_via_browser_shape(self, settings_client, settings_app):
        resp = self._browser_post(
            settings_client,
            [("gmail_enabled", ""), ("gmail_enabled", "on")],
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["gmail"]["enabled"] is True

    def test_gmail_enabled_unchecked_via_browser_shape(self, settings_client, settings_app):
        # Only the hidden submits when the box is unchecked.
        resp = self._browser_post(
            settings_client,
            [("gmail_enabled", "")],
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["sources"]["gmail"]["enabled"] is False

    def test_portal_search_master_persists_via_browser_shape(self, settings_client, settings_app):
        resp = self._browser_post(
            settings_client,
            [
                ("portal_search_enabled", ""),
                ("portal_search_enabled", "on"),
                ("portal_search_jobicy_enabled", ""),
                ("portal_search_jobicy_enabled", "on"),
                ("portal_search_yc_enabled", ""),
                ("portal_search_yc_enabled", "on"),
            ],
        )
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        ps = saved["sources"]["portal_search"]
        assert ps["enabled"] is True
        assert ps["jobicy"]["enabled"] is True
        assert ps["yc_workatastartup"]["enabled"] is True

    def test_all_checkbox_names_persist_true_via_browser_shape(
        self, settings_client, settings_app
    ):
        """Exhaustive: every checkbox in the form (16 names) must persist
        True when posted in the browser's hidden+checkbox shape.
        """
        names_and_paths = [
            ("gmail_enabled", ["sources", "gmail", "enabled"]),
            ("serpapi_enabled", ["sources", "serpapi", "enabled"]),
            ("thordata_enabled", ["sources", "thordata", "enabled"]),
            ("dataforseo_enabled", ["sources", "dataforseo", "enabled"]),
            ("google_cse_enabled", ["sources", "google_cse", "enabled"]),
            ("jsearch_enabled", ["sources", "jsearch", "enabled"]),
            ("portal_search_enabled", ["sources", "portal_search", "enabled"]),
            (
                "portal_search_jobicy_enabled",
                ["sources", "portal_search", "jobicy", "enabled"],
            ),
            (
                "portal_search_yc_enabled",
                ["sources", "portal_search", "yc_workatastartup", "enabled"],
            ),
            (
                "portal_search_usajobs_enabled",
                ["sources", "portal_search", "usajobs", "enabled"],
            ),
            (
                "portal_search_adzuna_enabled",
                ["sources", "portal_search", "adzuna", "enabled"],
            ),
            (
                "portal_search_jooble_enabled",
                ["sources", "portal_search", "jooble", "enabled"],
            ),
            (
                "notification_high_score",
                ["notifications", "high_score"],
            ),
            (
                "notification_pipeline_change",
                ["notifications", "pipeline_change"],
            ),
            (
                "notification_budget_alert",
                ["notifications", "budget_alert"],
            ),
            ("ats_scan_enabled", ["ats", "scan_enabled"]),
        ]
        fields = []
        for name, _ in names_and_paths:
            fields.append((name, ""))
            fields.append((name, "on"))
        resp = self._browser_post(settings_client, fields)
        assert resp.status_code == 302
        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        for name, path in names_and_paths:
            node = saved
            for key in path:
                assert key in node, f"missing path {path} for {name}: got {saved}"
                node = node[key]
            assert node is True, f"{name} (path {path}) persisted as {node!r}, expected True"


class TestSettingsDailyBudgetPersistence:
    """Regression: daily_budget_usd field was rendered in the UI but never
    parsed in _parse_form_to_config, so saving it silently did nothing.
    Issue #151.
    """

    def test_daily_budget_usd_saves_and_loads(self, settings_client, settings_app):
        """POST daily_budget_usd=7 → GET /settings/ shows 7 and config has 7.0."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "daily_budget_usd": "7",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)

        assert saved["scoring"]["daily_budget_usd"] == 7.0

    def test_daily_budget_usd_non_numeric_falls_back_to_default(
        self, settings_client, settings_app
    ):
        """Non-numeric budget input falls back to DEFAULT_DAILY_BUDGET_USD via safe_float."""
        from job_finder.config import DEFAULT_DAILY_BUDGET_USD

        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "daily_budget_usd": "not-a-number",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)

        assert saved["scoring"]["daily_budget_usd"] == DEFAULT_DAILY_BUDGET_USD

    def test_other_scoring_fields_still_save(self, settings_client, settings_app):
        """Regression: weights and thresholds still persist alongside daily_budget_usd."""
        resp = settings_client.post(
            "/settings/save",
            data={
                "target_titles": "Staff Data Scientist\nSenior Data Scientist",
                "profile_skills": "Python\nSQL\nSpark",
                "daily_budget_usd": "15",
                "min_score_threshold": "45",
                "candidate_score_threshold": "60",
            },
        )
        assert resp.status_code == 302

        with open(settings_app._test_config_path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)

        sc = saved["scoring"]
        assert sc["daily_budget_usd"] == 15.0
        assert sc["min_score_threshold"] == 45
        assert sc["candidate_score_threshold"] == 60
