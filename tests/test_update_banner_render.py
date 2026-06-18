"""Tests for update banner rendering in base.html."""

import pytest
from flask import Blueprint, render_template_string


@pytest.fixture
def tmp_path(tmp_path, monkeypatch):
    """Override user data dir for test isolation."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def stub_onboarding_blueprint(app):
    """Register stub /onboarding/welcome route for Test 5 (BLOCKER #2 fix)."""
    stub_bp = Blueprint("onboarding_stub", __name__)

    @stub_bp.route("/welcome")
    def welcome():
        return render_template_string(
            "{% extends 'base.html' %}{% block content %}stub{% endblock %}"
        )

    app.register_blueprint(stub_bp, url_prefix="/onboarding")
    yield
    # Blueprint stays registered for test duration


def test_banner_renders_when_update_available(client, tmp_path):
    """Test 1: banner renders on dashboard when cache says update available."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Update available: v5.0.1" in resp.data


def test_banner_not_render_when_current_equals_latest(client, tmp_path):
    """Test 2: banner does NOT render when cache says current == latest."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.0",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Update available" not in resp.data


def test_banner_not_render_when_version_dismissed(client, tmp_path):
    """Test 3: banner does NOT render when version is dismissed."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": ["v5.0.1"],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Update available" not in resp.data


def test_banner_not_render_with_no_cache(client, tmp_path):
    """Test 4: banner does NOT render with no cache."""
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Update available" not in resp.data


def test_banner_not_render_on_onboarding_welcome(client, app, tmp_path):
    """Test 5: banner does NOT render on /onboarding/welcome (path-prefix suppression)."""
    # Issue #400: the wizard's welcome route only renders mid-onboarding; a
    # completed install is redirected to /jobs by gate_completed_onboarding.
    # Reset the flag so the page renders and the banner-suppression assertion
    # is actually exercised.
    import sqlite3

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT INTO onboarding_state (id, onboarding_complete, wizard_data) "
            "VALUES (1, 0, '{}') "
            "ON CONFLICT(id) DO UPDATE SET onboarding_complete = 0"
        )
        conn.commit()
    finally:
        conn.close()

    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/onboarding/welcome")
    assert resp.status_code == 200
    assert b"Update available" not in resp.data


def test_kick_off_not_called_when_testing_true(client, tmp_path, monkeypatch):
    """Test 6: kick_off_background_check_if_due NOT called when TESTING=True."""
    called = []

    def mock_kick_off(config):
        if config and config.get("TESTING"):
            return
        called.append(True)

    monkeypatch.setattr(
        "job_finder.web.update_check.kick_off_background_check_if_due", mock_kick_off
    )
    # Don't follow redirects to avoid multiple before_request calls
    client.get("/")
    assert len(called) == 0


def test_dismiss_button_wired_correctly(client, tmp_path):
    """Test 7: dismiss button is wired to /updates/dismiss/<version>."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert b'hx-post="/updates/dismiss/v5.0.1"' in resp.data
    assert b'hx-target="#update-banner"' in resp.data
    assert b'hx-swap="delete"' in resp.data


def test_banner_href_targets_github_releases_tag(client, tmp_path):
    """Test 8: banner href targets GitHub releases tag."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert b'href="https://github.com/Senkichi/job-cannon/releases/tag/v5.0.1"' in resp.data


def test_banner_has_rel_noopener_target_blank(client, tmp_path):
    """Test 9: banner has rel=noopener and target=_blank."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    assert b'target="_blank"' in resp.data
    assert b'rel="noopener"' in resp.data


def test_latest_version_html_escaped(client, tmp_path):
    """Test 10: latest_version is HTML-escaped in render."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1<script>",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    from job_finder.web import update_check

    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    resp = client.get("/", follow_redirects=True)
    # Check that the version string in the banner is escaped
    assert b"v5.0.1&lt;script&gt;" in resp.data
    # Check that the raw version string does NOT appear in the banner link
    assert (
        b'href="https://github.com/Senkichi/job-cannon/releases/tag/v5.0.1<script>"'
        not in resp.data
    )
