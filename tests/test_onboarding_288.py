"""Tests for Issue #288 wizard provider detection improvements.

Acceptance criteria:
- Ollama-no-model state renders pull guidance in provider_select
- Skip path (skip_provider POST) writes provider.name="none", redirects to resume_upload
- Wizard completes with zero providers selected and writes a valid config
  (passes load_config() without error)
- provider_credentials renders "no credentials needed" for provider="none"
- HTMX conventions: full page returned for direct browser GET (HX-Request absent)
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import yaml

from job_finder.config import load_config
from job_finder.web.providers.detection import DetectionExtras

# ---------------------------------------------------------------------------
# Shared: suppress live provider detection in all route tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_live_detection():
    """Prevent real CLI probes during route tests."""
    with (
        patch(
            "job_finder.web.onboarding.blueprint.detect_available_providers",
            return_value=[],
        ),
        patch(
            "job_finder.web.onboarding.blueprint.get_detection_extras",
            return_value=DetectionExtras(ollama_no_model=False),
        ),
        patch(
            "job_finder.web.providers.detection.detect_available_providers",
            return_value=[],
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# provider_select template: Ollama no-model guidance
# ---------------------------------------------------------------------------


def test_provider_select_renders_ollama_no_model_guidance(client):
    """When ollama_no_model=True, provider_select must render inline pull guidance."""
    with (
        patch(
            "job_finder.web.onboarding.blueprint.detect_available_providers",
            return_value=[],
        ),
        patch(
            "job_finder.web.onboarding.blueprint.get_detection_extras",
            return_value=DetectionExtras(ollama_no_model=True),
        ),
    ):
        resp = client.get("/onboarding/provider_select")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ollama pull qwen2.5:14b" in body
    assert (
        "no models" in body.lower()
        or "has no models" in body.lower()
        or "Ollama is installed but has no models" in body
    )


def test_provider_select_no_ollama_guidance_when_flag_false(client):
    """When ollama_no_model=False, the pull guidance block must NOT appear."""
    resp = client.get("/onboarding/provider_select")
    body = resp.get_data(as_text=True)
    assert "ollama pull qwen2.5:14b" not in body


# ---------------------------------------------------------------------------
# Skip path (Issue #288 acceptance criterion)
# ---------------------------------------------------------------------------


def test_skip_provider_post_writes_none_and_redirects_to_resume_upload(client, app):
    """POST skip_provider=1 must write wizard_data provider.name='none' and redirect to resume_upload."""
    resp = client.post("/onboarding/provider_select", data={"skip_provider": "1"})
    assert resp.status_code == 302
    assert "/onboarding/resume_upload" in resp.headers["Location"], (
        f"Expected redirect to resume_upload, got: {resp.headers['Location']}"
    )

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["provider"]["name"] == "none", (
            f"Expected provider.name='none', got: {data['provider']['name']}"
        )
    finally:
        conn.close()


def test_skip_provider_bypasses_provider_credentials(client):
    """Skip path must not route through provider_credentials (redirects directly to resume_upload)."""
    resp = client.post("/onboarding/provider_select", data={"skip_provider": "1"})
    assert resp.status_code == 302
    # Must go to resume_upload, NOT provider_credentials
    assert "/onboarding/provider_credentials" not in resp.headers["Location"]
    assert "/onboarding/resume_upload" in resp.headers["Location"]


def test_provider_credentials_no_key_for_none_provider(client, app):
    """If wizard_data has provider.name='none', provider_credentials must render no-creds card."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"none"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/provider_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Must render the no-credentials card, not the API key form
    assert 'type="password"' not in body
    assert "No credentials needed" in body


# ---------------------------------------------------------------------------
# Skip path end-to-end: wizard completes, config passes load_config()
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_app_288(app, tmp_path, monkeypatch):
    """App fixture with tmp_path as user_data_root, no existing config."""
    cfg_path = tmp_path / "config.yaml"
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


def _seed_wizard(db_path: str, payload: dict) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state"
            " (id, onboarding_complete, wizard_data) VALUES (1, 0, ?)",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()


_NO_PROVIDER_WIZARD_PAYLOAD = {
    "provider": {"name": "none"},
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "email": "",
        "app_password": "",
        "folder": "INBOX",
        "enabled": False,
        "verified": False,
    },
    "profile_edit": {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_locations": "Remote",
        "skills": "python\nsqlite",
    },
    "resume_profile": {},
    "schedule": {"cadence_preset": "standard"},
}


def test_skip_provider_done_post_redirects_to_jobs(fresh_app_288, monkeypatch):
    """POST /onboarding/done with provider.name='none' must return 302 to /jobs."""
    _seed_wizard(fresh_app_288.config["DB_PATH"], _NO_PROVIDER_WIZARD_PAYLOAD)

    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=MagicMock(),
    ):
        resp = fresh_app_288.test_client().post("/onboarding/done")

    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]


def test_skip_provider_written_config_passes_load_config(fresh_app_288, monkeypatch):
    """Core acceptance criterion (Issue #288): config written via no-provider skip path
    must pass load_config() without raising ConfigError or ValueError."""
    _seed_wizard(fresh_app_288.config["DB_PATH"], _NO_PROVIDER_WIZARD_PAYLOAD)

    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=MagicMock(),
    ):
        fresh_app_288.test_client().post("/onboarding/done")

    cfg_path = fresh_app_288._test_cfg_path
    assert cfg_path.exists(), "config.yaml was not written"

    # Must not raise
    cfg = load_config(str(cfg_path))
    assert cfg is not None

    # providers.primary must be absent (not "none") when skipped
    providers = cfg.get("providers", {})
    primary = providers.get("primary", "")
    assert primary != "none", (
        f"providers.primary should be empty/absent when skipped, got: {primary!r}"
    )


def test_skip_provider_config_has_required_sections(fresh_app_288, monkeypatch):
    """Config written via skip path must contain all required sections: profile, sources, scoring, db."""
    _seed_wizard(fresh_app_288.config["DB_PATH"], _NO_PROVIDER_WIZARD_PAYLOAD)

    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=MagicMock(),
    ):
        fresh_app_288.test_client().post("/onboarding/done")

    with open(fresh_app_288._test_cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    for section in ("profile", "sources", "scoring", "db"):
        assert section in cfg, f"Required section '{section}' missing from written config"


def test_skip_provider_config_target_titles_preserved(fresh_app_288, monkeypatch):
    """profile.target_titles from profile_edit must be written even on skip path."""
    _seed_wizard(fresh_app_288.config["DB_PATH"], _NO_PROVIDER_WIZARD_PAYLOAD)

    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=MagicMock(),
    ):
        fresh_app_288.test_client().post("/onboarding/done")

    with open(fresh_app_288._test_cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    titles = cfg.get("profile", {}).get("target_titles", [])
    assert "Staff Engineer" in titles
    assert "Principal Engineer" in titles


# ---------------------------------------------------------------------------
# provider_select template: skip button always present
# ---------------------------------------------------------------------------


def test_provider_select_always_has_skip_button(client):
    """Skip button must appear on provider_select whether or not providers are detected."""
    resp = client.get("/onboarding/provider_select")
    body = resp.get_data(as_text=True)
    assert "skip_provider" in body
    assert "Skip" in body


def test_provider_select_with_providers_has_skip_link(client):
    """Even when providers are detected, a skip link must be present."""
    from job_finder.web.providers.detection import ProviderHandle

    mock_provider = ProviderHandle(
        name="claude_code_cli",
        binary_path="/usr/bin/claude",
        cost_label="$0",
        priority=1,
    )
    with (
        patch(
            "job_finder.web.onboarding.blueprint.detect_available_providers",
            return_value=[mock_provider],
        ),
        patch(
            "job_finder.web.onboarding.blueprint.get_detection_extras",
            return_value=DetectionExtras(ollama_no_model=False),
        ),
    ):
        resp = client.get("/onboarding/provider_select")

    body = resp.get_data(as_text=True)
    assert "skip_provider" in body or "configure later" in body.lower()
