"""Tests for Issue #299 and #300 wizard correctness fixes.

#299 — Three fuses that brick a fresh-install on restart:
  B1. Done step omitted scoring/db from config_slice → load_config raises on restart.
  B2. Empty target_titles accepted → validate_target_titles raises on restart.
  B3. Gmail Skip wrote enabled:true → spurious IMAP errors every ingest.

#300 — Wizard never refreshed JF_CONFIG → first ingest saw {} → 0 jobs, silently.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from job_finder.config import load_config

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_app(app, tmp_path, monkeypatch):
    """App fixture where user_data_dirs points at tmp_path and config.yaml does NOT exist
    (simulates a genuine fresh install — merge base is {}).
    """
    cfg_path = tmp_path / "config.yaml"
    # Do NOT touch() the file — it must be absent so load_config(allow_missing=True) → {}

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.config_path",
        lambda: cfg_path,
    )
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.user_data_root",
        lambda: tmp_path,
    )
    app._test_cfg_path = cfg_path
    app._test_tmp_path = tmp_path
    return app


def _seed_wizard(db_path: str, payload: dict, *, complete: int = 0) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state"
            " (id, onboarding_complete, wizard_data) VALUES (1, ?, ?)",
            (complete, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


_FULL_WIZARD_PAYLOAD = {
    "provider": {"name": "ollama"},
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "email": "user@example.com",
        "app_password": "xxxx xxxx xxxx xxxx",
        "folder": "INBOX",
        "enabled": True,
        "verified": True,
    },
    "profile_edit": {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_locations": "Remote",
        "skills": "python\nsqlite",
    },
    "resume_profile": {},
    "schedule": {"cadence_preset": "standard"},
}


# ---------------------------------------------------------------------------
# Issue #299 — Fuse 1: missing scoring + db sections on fresh install
# ---------------------------------------------------------------------------


class TestFreshInstallConfigValid:
    """Completing the wizard against an empty merge base must produce a config that
    passes load_config() without error on the next restart."""

    def test_written_config_passes_load_config(self, fresh_app):
        """Core acceptance criterion: load_config on the written file must not raise."""
        _seed_wizard(fresh_app.config["DB_PATH"], _FULL_WIZARD_PAYLOAD)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            resp = fresh_app.test_client().post("/onboarding/done")

        assert resp.status_code == 302, "done POST should redirect"

        cfg_path: Path = fresh_app._test_cfg_path
        assert cfg_path.exists(), "config.yaml was not written"

        # This must not raise — if it does the app would crash on every restart.
        loaded = load_config(str(cfg_path))
        assert loaded is not None

    def test_written_config_contains_scoring_section(self, fresh_app):
        _seed_wizard(fresh_app.config["DB_PATH"], _FULL_WIZARD_PAYLOAD)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            fresh_app.test_client().post("/onboarding/done")

        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        assert "scoring" in written, "scoring section missing from written config"
        assert "daily_budget_usd" in written["scoring"], (
            "scoring.daily_budget_usd missing — validate_required_sections will "
            "pass but scoring behavior will be unconfigured"
        )

    def test_written_config_contains_db_section(self, fresh_app):
        _seed_wizard(fresh_app.config["DB_PATH"], _FULL_WIZARD_PAYLOAD)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            fresh_app.test_client().post("/onboarding/done")

        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        assert "db" in written, "db section missing from written config"
        assert "path" in written["db"], "db.path missing"
        # The written db.path must match the running app's DB_PATH so it stays
        # consistent with the live process (not a hardcoded "jobs.db" stub).
        assert written["db"]["path"] == fresh_app.config["DB_PATH"], (
            "db.path in written config does not match app.config['DB_PATH']"
        )

    def test_existing_scoring_section_preserved_on_merge(self, fresh_app):
        """When an existing config already has a fuller scoring block, the wizard
        merge must not overwrite it with the minimal defaults."""
        existing = {
            "providers": {"primary": "ollama"},
            "sources": {"imap": {"enabled": False}},
            "profile": {"target_titles": ["Engineer"], "skills": ["python"]},
            "scoring": {
                "daily_budget_usd": 42.0,
                "min_score_threshold": 55,
                "candidate_score_threshold": 60,
            },
            "db": {"path": "custom.db"},
        }
        fresh_app._test_cfg_path.write_text(yaml.safe_dump(existing), encoding="utf-8")

        _seed_wizard(fresh_app.config["DB_PATH"], _FULL_WIZARD_PAYLOAD)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            fresh_app.test_client().post("/onboarding/done")

        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        # daily_budget_usd from existing wins (deep merge: existing values not in
        # slice are preserved; slice values fill in where absent).
        assert written["scoring"]["daily_budget_usd"] == 42.0, (
            "_deep_merge should preserve existing daily_budget_usd"
        )
        assert written["scoring"]["min_score_threshold"] == 55, (
            "existing min_score_threshold was overwritten"
        )
        assert written["db"]["path"] == "custom.db", (
            "existing db.path overwritten by wizard default"
        )


# ---------------------------------------------------------------------------
# Issue #299 — Fuse 2: empty target_titles must be rejected server-side
# ---------------------------------------------------------------------------


class TestProfileEditEmptyTitles:
    """profile_edit POST with empty target_titles must be rejected — never write
    wizard_data with an empty titles list."""

    def test_empty_titles_returns_200_with_error(self, app):
        """Submitting blank target_titles must re-render the form (200) with an error."""
        _seed_wizard(
            app.config["DB_PATH"],
            {"provider": {"name": "ollama"}, "resume_profile": {}},
        )
        client = app.test_client()
        resp = client.post(
            "/onboarding/profile_edit",
            data={
                "target_titles": "",
                "target_locations": "Remote",
                "skills": "python",
                "min_salary": "",
            },
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "required" in body.lower() or "title" in body.lower(), (
            "error message about target titles not found in response"
        )

    def test_empty_titles_does_not_advance_to_next_step(self, app):
        """Empty target_titles must NOT redirect to imap_credentials."""
        _seed_wizard(
            app.config["DB_PATH"],
            {"provider": {"name": "ollama"}, "resume_profile": {}},
        )
        resp = app.test_client().post(
            "/onboarding/profile_edit",
            data={"target_titles": "   ", "target_locations": "Remote", "skills": ""},
        )
        # Must not be a redirect to imap_credentials
        assert resp.status_code == 200, (
            f"Expected 200 re-render, got {resp.status_code} — "
            "empty titles caused an advance to the next step"
        )

    def test_empty_titles_does_not_write_wizard_data(self, app):
        """Empty target_titles must leave wizard_data unchanged — no partial write."""
        initial_payload = {"provider": {"name": "ollama"}, "resume_profile": {}}
        _seed_wizard(app.config["DB_PATH"], initial_payload)

        app.test_client().post(
            "/onboarding/profile_edit",
            data={"target_titles": "", "target_locations": "Remote", "skills": ""},
        )

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        finally:
            conn.close()

        stored = json.loads(row[0])
        # profile_edit key must not have been written with empty titles
        profile_edit = stored.get("profile_edit") or {}
        titles = profile_edit.get("target_titles", "SENTINEL")
        assert titles in ("", "SENTINEL", None), (
            f"wizard_data was written with target_titles={titles!r} despite empty submission"
        )

    def test_whitespace_only_titles_rejected(self, app):
        """Whitespace-only input (spaces/tabs/newlines) must be treated as empty."""
        _seed_wizard(
            app.config["DB_PATH"],
            {"provider": {"name": "ollama"}, "resume_profile": {}},
        )
        resp = app.test_client().post(
            "/onboarding/profile_edit",
            data={"target_titles": "\n  \t\n", "target_locations": "Remote"},
        )
        assert resp.status_code == 200

    def test_valid_titles_advance_to_next_step(self, app):
        """Non-empty target_titles must still redirect to imap_credentials (regression guard)."""
        _seed_wizard(
            app.config["DB_PATH"],
            {"provider": {"name": "ollama"}, "resume_profile": {}},
        )
        resp = app.test_client().post(
            "/onboarding/profile_edit",
            data={
                "target_titles": "Staff Engineer",
                "target_locations": "Remote",
                "skills": "python",
                "min_salary": "",
            },
        )
        assert resp.status_code == 302
        assert "imap_credentials" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Issue #299 — Fuse 3: Gmail Skip must write enabled:false
# ---------------------------------------------------------------------------


class TestGmailSkipDisablesImap:
    """Clicking Skip on the IMAP step must persist enabled:false, not enabled:true."""

    def test_skip_writes_imap_enabled_false(self, app):
        _seed_wizard(app.config["DB_PATH"], {"provider": {"name": "ollama"}})
        app.test_client().post(
            "/onboarding/imap_credentials",
            data={"skip": "1", "email": "", "app_password": ""},
        )

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        finally:
            conn.close()

        stored = json.loads(row[0])
        imap = stored.get("imap", {})
        assert imap.get("enabled") is False, (
            f"Gmail Skip must write imap.enabled=False, got {imap.get('enabled')!r}"
        )

    def test_skip_verified_false(self, app):
        """Skip must also set verified:false so the field is explicit."""
        _seed_wizard(app.config["DB_PATH"], {"provider": {"name": "ollama"}})
        app.test_client().post(
            "/onboarding/imap_credentials",
            data={"skip": "1", "email": "", "app_password": ""},
        )

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        finally:
            conn.close()

        stored = json.loads(row[0])
        assert stored["imap"]["verified"] is False

    def test_skip_propagates_to_written_config(self, fresh_app):
        """When the whole wizard completes after a Skip, sources.imap.enabled must be False
        in the written config.yaml."""
        payload = dict(_FULL_WIZARD_PAYLOAD)
        payload = {
            **_FULL_WIZARD_PAYLOAD,
            "imap": {
                "host": "imap.gmail.com",
                "port": 993,
                "email": "",
                "app_password": "",
                "folder": "INBOX",
                "enabled": False,  # as written by Skip
                "verified": False,
            },
        }
        _seed_wizard(fresh_app.config["DB_PATH"], payload)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            fresh_app.test_client().post("/onboarding/done")

        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        assert written["sources"]["imap"]["enabled"] is False, (
            "sources.imap.enabled should be False when user skipped IMAP setup"
        )

    def test_successful_imap_test_writes_enabled_true(self, app):
        """Regression guard: a successful IMAP smoke test must still write enabled:true."""
        _seed_wizard(app.config["DB_PATH"], {"provider": {"name": "ollama"}})

        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.message = "OK"

        with patch(
            "job_finder.web.onboarding.blueprint.imap_test.check_imap",
            return_value=mock_result,
        ):
            app.test_client().post(
                "/onboarding/imap_credentials",
                data={
                    "email": "u@example.com",
                    "app_password": "xxxx xxxx xxxx xxxx",
                },
            )

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        finally:
            conn.close()

        stored = json.loads(row[0])
        assert stored["imap"]["enabled"] is True, "Successful IMAP test must set enabled:true"


# ---------------------------------------------------------------------------
# Issue #300 — JF_CONFIG refresh after wizard Done
# ---------------------------------------------------------------------------


class TestJfConfigRefreshedAfterDone:
    """After POST /onboarding/done, app.config['JF_CONFIG'] must reflect the
    written config so the first-ingest closure sees real sources, not {}."""

    def test_jf_config_updated_in_app_after_done(self, fresh_app):
        """JF_CONFIG must not be {} after wizard completion."""
        # App starts with JF_CONFIG = {} (simulating fresh install boot)
        fresh_app.config["JF_CONFIG"] = {}

        _seed_wizard(fresh_app.config["DB_PATH"], _FULL_WIZARD_PAYLOAD)

        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            resp = fresh_app.test_client().post("/onboarding/done")

        assert resp.status_code == 302
        live_cfg = fresh_app.config["JF_CONFIG"]
        assert live_cfg, "JF_CONFIG is still {} after wizard Done — refresh not called"
        assert "providers" in live_cfg, "JF_CONFIG missing providers after refresh"
        assert "scoring" in live_cfg, "JF_CONFIG missing scoring after refresh"
        assert "db" in live_cfg, "JF_CONFIG missing db after refresh"

    def test_first_ingest_closure_sees_written_sources(self, fresh_app):
        """The first-ingest closure must use the post-write JF_CONFIG, not the boot-time {}."""
        fresh_app.config["JF_CONFIG"] = {}

        imap_payload = dict(_FULL_WIZARD_PAYLOAD)
        # IMAP is enabled:true in the full payload
        _seed_wizard(fresh_app.config["DB_PATH"], imap_payload)

        mock_scheduler = MagicMock()
        mock_run_ingestion = MagicMock(
            return_value={
                "jobs_new": 0,
                "gmail_fetched": 0,
                "serpapi_fetched": 0,
                "thordata_fetched": 0,
                "dataforseo_fetched": 0,
            }
        )

        with (
            patch(
                "job_finder.web.onboarding.blueprint.get_scheduler",
                return_value=mock_scheduler,
            ),
            patch(
                "job_finder.web.pipeline_runner.run_ingestion",
                mock_run_ingestion,
            ),
        ):
            fresh_app.test_client().post("/onboarding/done")

        # Invoke the closure as APScheduler would
        scheduled_callable = mock_scheduler.add_job.call_args.args[0]
        with patch("job_finder.web.pipeline_runner.run_ingestion", mock_run_ingestion):
            scheduled_callable()

        mock_run_ingestion.assert_called_once()
        _, config_arg = mock_run_ingestion.call_args.args
        assert config_arg.get("sources"), (
            "run_ingestion received empty/missing sources — "
            "JF_CONFIG was not refreshed before the closure captured app_obj"
        )
        assert config_arg["sources"].get("imap", {}).get("enabled") is True, (
            "First-ingest closure did not see imap.enabled=True from the written config"
        )

    def test_settings_save_uses_refresh_helper(self, app, tmp_path, monkeypatch):
        """Settings save route must call refresh_jf_config (shared helper).
        Verified by confirming JF_CONFIG changes after a settings POST.
        """

        cfg_path = tmp_path / "config.yaml"
        healthy = {
            "providers": {"primary": "ollama", "fallback_chain": []},
            "sources": {"imap": {"enabled": False}},
            "profile": {
                "target_titles": ["Staff Engineer"],
                "target_locations": ["Remote"],
                "skills": ["python"],
            },
            "scoring": {"daily_budget_usd": 10.0, "min_score_threshold": 40},
            "db": {"path": str(app.config["DB_PATH"])},
        }
        cfg_path.write_text(yaml.safe_dump(healthy), encoding="utf-8")

        # Point the settings blueprint at this config
        monkeypatch.setattr(
            "job_finder.web.blueprints.settings._CONFIG_PATH",
            str(cfg_path),
        )

        app.config["JF_CONFIG"] = healthy

        # POST to settings/save with a changed cadence — we just need any valid form
        # that round-trips through the save route and calls refresh_jf_config.
        # The route reads _CONFIG_PATH, merges, writes, then calls refresh_jf_config.
        # We verify by checking JF_CONFIG was updated.
        with (
            patch(
                "job_finder.web.blueprints.settings._write_config",
            ) as mock_write,
            patch(
                "job_finder.web.blueprints.settings.load_config",
                return_value=dict(healthy),
            ),
        ):
            # Inject a sentinel value to detect the refresh
            def fake_write(cfg, path):
                cfg["_sentinel"] = "refreshed"

            mock_write.side_effect = fake_write

            # Patch refresh_jf_config to capture the config it receives
            refreshed_with = {}

            def fake_refresh(app_instance, cfg):
                refreshed_with.update(cfg)
                app_instance.config["JF_CONFIG"] = cfg

            with patch(
                "job_finder.web.blueprints.settings.refresh_jf_config",
                side_effect=fake_refresh,
            ):
                app.test_client().post(
                    "/settings/save",
                    data={
                        "profile.target_titles": "Staff Engineer",
                        "profile.target_locations": "Remote",
                        "profile.skills": "python",
                        "profile.min_salary": "150000",
                        "scoring.min_score_threshold": "40",
                        "scoring.daily_budget_usd": "10.0",
                        "providers.primary": "ollama",
                    },
                )

        assert refreshed_with, (
            "refresh_jf_config was never called from settings save — "
            "JF_CONFIG would not update after a settings change"
        )

    def test_refresh_jf_config_swaps_config_atomically(self, app, caplog):
        """The common path: config carries the live DB_PATH (both real callers
        copy it back in), so JF_CONFIG is swapped in one assignment with no
        warning and DB_PATH is unchanged."""
        import logging

        from job_finder.web.db_helpers import refresh_jf_config

        current = app.config["DB_PATH"]
        cfg = {
            "db": {"path": current},
            "scoring": {},
            "profile": {"target_titles": ["Eng"]},
        }
        with (
            app.app_context(),
            caplog.at_level(logging.WARNING, logger="job_finder.web.db_helpers"),
        ):
            refresh_jf_config(app, cfg)

        assert app.config["JF_CONFIG"] is cfg
        assert app.config["DB_PATH"] == current
        assert "fixed for this process" not in caplog.text

    def test_refresh_jf_config_keeps_db_path_immutable(self, app, caplog):
        """DB_PATH is the process-wide authority and is fixed at create_app()
        time. A differing db.path (a live relocation attempt) warns and defers
        to restart rather than half-applying a torn cross-key write."""
        import logging

        from job_finder.web.db_helpers import refresh_jf_config

        original = app.config["DB_PATH"]
        cfg = {
            "db": {"path": "/tmp/relocated_jobs.db"},
            "scoring": {},
            "profile": {"target_titles": ["Eng"]},
        }
        with (
            app.app_context(),
            caplog.at_level(logging.WARNING, logger="job_finder.web.db_helpers"),
        ):
            refresh_jf_config(app, cfg)

        assert app.config["JF_CONFIG"] is cfg
        # NOT mutated live — relocation requires a restart.
        assert app.config["DB_PATH"] == original
        assert "fixed for this process" in caplog.text
