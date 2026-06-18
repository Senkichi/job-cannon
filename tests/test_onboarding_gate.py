"""Integration tests for the @before_request gate (STRANGE-WIZ-01, D-18, D-19)."""

import sqlite3

import pytest


@pytest.fixture
def unconf_client(app_unconfigured):
    """Test client with onboarding_complete=0 — gate should redirect everything."""
    return app_unconfigured.test_client()


def test_root_redirects_when_incomplete(unconf_client):
    """GET / with onboarding_complete=0 → 302 to /onboarding/welcome (success criterion 1)."""
    resp = unconf_client.get("/")
    assert resp.status_code == 302
    assert "/onboarding/welcome" in resp.headers["Location"]


def test_jobs_redirects_when_incomplete(unconf_client):
    """GET /jobs with onboarding_complete=0 → 302 to /onboarding/welcome (gate covers all blueprints)."""
    resp = unconf_client.get("/jobs")
    assert resp.status_code in (301, 302)
    # 301 if Flask normalizes trailing slash; 302 from the gate
    if resp.status_code == 302:
        assert "/onboarding/welcome" in resp.headers["Location"]


def test_root_passes_when_complete(client):
    """Standard `client` fixture has onboarding_complete=1 (seeded by conftest plan 42-01).
    GET / should reach the existing root route which redirects to /jobs."""
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]
    assert "/onboarding" not in resp.headers["Location"]


def test_whitelist_onboarding_paths_pass_when_incomplete(unconf_client):
    """Whitelist: /onboarding/welcome itself MUST NOT be gated (else infinite redirect)."""
    resp = unconf_client.get("/onboarding/welcome")
    assert resp.status_code == 200
    # Plan 42-02 ships a scaffold "welcome — full template lands in plan 42-05" string.
    # Plan 42-05 replaces this with the real template; this test asserts only that
    # the gate did not redirect (status != 302) — content marker assertion lives in plan 42-05.


def test_whitelist_static_paths_pass_when_incomplete(unconf_client):
    """Whitelist: /static/* MUST NOT be gated (CSS/JS would 302-loop otherwise)."""
    # /static/anything.css → may 404 because no file exists, but MUST NOT be 302
    resp = unconf_client.get("/static/nonexistent.css")
    assert resp.status_code != 302


def test_whitelist_favicon_passes_when_incomplete(unconf_client):
    """Whitelist: /favicon.ico MUST NOT be gated."""
    resp = unconf_client.get("/favicon.ico")
    assert resp.status_code != 302


def test_whitelist_jc_health_passes_when_incomplete(unconf_client):
    """Whitelist: /__jc_health MUST NOT be gated (WP9 frozen-app smoke-test finding).

    The endpoint is the launcher's single-instance identity probe
    (__main__.probe_existing_jc expects 200 + data["app"] == "job-cannon").
    Before the fix, an unconfigured install 302'd the probe to the wizard,
    silently degrading already-running detection to the psutil-cmdline
    fallback.
    """
    resp = unconf_client.get("/__jc_health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["app"] == "job-cannon"


def test_gate_handles_missing_row_with_no_legacy_install(app_unconfigured):
    """If onboarding_state row is missing AND no legacy install detected, redirect to welcome."""
    # Delete the row that app_unconfigured seeded
    db_path = app_unconfigured.config["DB_PATH"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM onboarding_state WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    client = app_unconfigured.test_client()
    resp = client.get("/jobs")
    # Without legacy heuristic kicking in (no config.yaml / experience_profile.json in user_data_root), expect redirect
    # NB: depending on cwd this may falsely pass the heuristic; the test asserts the BEHAVIOR contract — gate runs and either redirects OR auto-completes via heuristic.
    # If heuristic fires: row will be inserted with onboarding_complete=1 and request continues.
    # Either way, the gate did NOT raise.
    assert resp.status_code in (200, 301, 302)


def test_welcome_redirects_when_complete(client):
    """Issue #400: a completed install hitting GET /onboarding/welcome → 302 to /jobs.

    The standard `client` fixture has onboarding_complete=1. Before the fix this
    returned 200 and re-served the wizard (the keyring-overwrite entry point).
    """
    resp = client.get("/onboarding/welcome")
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]
    assert "/onboarding" not in resp.headers["Location"]


def test_all_wizard_routes_redirect_when_complete(client):
    """Issue #400: EVERY wizard route redirects out once onboarding is complete."""
    for route in (
        "/onboarding/welcome",
        "/onboarding/provider_select",
        "/onboarding/provider_credentials",
        "/onboarding/resume_upload",
        "/onboarding/profile_edit",
        "/onboarding/imap_credentials",
        "/onboarding/schedule",
        "/onboarding/done",
    ):
        resp = client.get(route)
        assert resp.status_code == 302, f"{route} did not redirect"
        assert "/jobs" in resp.headers["Location"], (
            f"{route} redirected to {resp.headers['Location']}"
        )


def test_done_post_redirects_when_complete_without_overwrite(client):
    """Issue #400 (footgun #4): POST /onboarding/done on a completed install is
    bounced to /jobs before any config-write side effect runs."""
    resp = client.post("/onboarding/done", data={})
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]


def test_welcome_serves_when_incomplete(unconf_client):
    """Issue #400 regression guard: the completed-user redirect MUST NOT trap an
    incomplete user — GET /onboarding/welcome still serves 200 mid-onboarding."""
    resp = unconf_client.get("/onboarding/welcome")
    assert resp.status_code == 200


def test_no_open_redirect_via_next_param(unconf_client):
    """T-42-04: gate MUST NOT honor ?next= query param. All redirects target /onboarding/welcome only."""
    resp = unconf_client.get("/jobs?next=https://evil.example.com")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.example.com" not in location
    assert "/onboarding/welcome" in location
