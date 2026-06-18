"""Integration tests for the onboarding blueprint routes (STRANGE-WIZ-02 + STRANGE-WIZ-05).

Parametrized GET smoke covers all 8 routes (success criterion 2 — each renders with a
unique marker string). Targeted tests cover resume upload security (T-42-03/T-42-07),
IMAP failure re-render (D-08), and wizard_data write semantics (D-13).
"""

import io
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mid_onboarding(app):
    """Walk the wizard as an *in-progress* user (Issue #400).

    These route tests exercise wizard rendering/navigation, which is only a
    reachable state while onboarding is incomplete. The standard `app`/`client`
    fixtures seed onboarding_complete=1, and the blueprint-level
    gate_completed_onboarding now bounces a completed user out of every wizard
    route to /jobs. Reset the flag to 0 so the wizard is walkable here; the
    completed-user redirect itself is covered by tests/test_onboarding_gate.py.
    """
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT INTO onboarding_state (id, onboarding_complete, wizard_data) "
            "VALUES (1, 0, '{}') "
            "ON CONFLICT(id) DO UPDATE SET onboarding_complete = 0"
        )
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture(autouse=True)
def _no_live_provider_detection():
    """Stop onboarding routes from spawning real CLI liveness probes.

    GET /onboarding/provider_select (and the welcome system-check) call
    providers.detection.detect_available_providers(), which runs
    `claude -p ping` / `gemini -p ping` / `ollama list` subprocesses with a 10s
    timeout each — ~15s on a machine where those CLIs are installed, and
    environment-dependent. These route tests only need the page + its marker;
    the detection logic itself is covered by tests/test_provider_detection.py.
    Patch the names bound in each onboarding module that imports it; return [].
    """
    with (
        patch(
            "job_finder.web.onboarding.blueprint.detect_available_providers",
            return_value=[],
        ),
        patch(
            "job_finder.web.providers.detection.detect_available_providers",
            return_value=[],
        ),
    ):
        yield


# The 8 routes + their marker strings (success criterion 2)
_ROUTE_TO_MARKER = [
    ("/onboarding/welcome", "WIZARD_STEP_WELCOME_MARKER"),
    ("/onboarding/provider_select", "WIZARD_STEP_PROVIDER_SELECT_MARKER"),
    ("/onboarding/provider_credentials", "WIZARD_STEP_PROVIDER_CREDENTIALS_MARKER"),
    ("/onboarding/resume_upload", "WIZARD_STEP_RESUME_UPLOAD_MARKER"),
    ("/onboarding/profile_edit", "WIZARD_STEP_PROFILE_EDIT_MARKER"),
    ("/onboarding/imap_credentials", "WIZARD_STEP_IMAP_CREDENTIALS_MARKER"),
    ("/onboarding/schedule", "WIZARD_STEP_SCHEDULE_MARKER"),
    ("/onboarding/done", "WIZARD_STEP_DONE_MARKER"),
]


@pytest.mark.parametrize("path,marker", _ROUTE_TO_MARKER)
def test_each_step_renders(client, path, marker):
    """GET each step returns 200 + contains the unique marker string (success criterion 2)."""
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"
    body = resp.get_data(as_text=True)
    assert marker in body, f"{path} response missing marker {marker}"


def test_welcome_renders_system_check_results(client):
    """Welcome GET invokes system_check.run_all() and renders each result.

    M-3 (2026-05-20): port-free check was dropped (it always reported the
    wizard's own port 5000 as in-use). Only the two remaining checks render.
    """
    resp = client.get("/onboarding/welcome")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "DB writable" in body
    assert "Network reachable" in body
    # The deleted port check no longer renders.
    assert "Port 5000" not in body


def test_step_indicator_renders(client):
    """Step indicator passes step_num and step_label to _base.html."""
    resp = client.get("/onboarding/welcome")
    body = resp.get_data(as_text=True)
    assert "Step 1 of 8" in body
    assert "Welcome" in body

    resp = client.get("/onboarding/schedule")
    body = resp.get_data(as_text=True)
    assert "Step 7 of 8" in body
    assert "Schedule" in body


def test_welcome_post_redirects_to_provider_select(client):
    """POST /onboarding/welcome → 302 to /onboarding/provider_select."""
    resp = client.post("/onboarding/welcome", data={})
    assert resp.status_code == 302
    assert "/onboarding/provider_select" in resp.headers["Location"]


def test_provider_select_post_writes_wizard_data_and_redirects(client, app):
    """POST /onboarding/provider_select writes {provider: {name}} to wizard_data, redirects to credentials."""
    resp = client.post("/onboarding/provider_select", data={"provider_name": "ollama"})
    assert resp.status_code == 302
    assert "/onboarding/provider_credentials" in resp.headers["Location"]

    # Verify wizard_data was written
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        assert row is not None
        import json

        data = json.loads(row[0])
        assert data["provider"]["name"] == "ollama"
    finally:
        conn.close()


def test_provider_credentials_no_creds_for_free_clis(client, app):
    """provider_credentials GET for ollama renders the 'no credentials needed' card."""
    # First write a provider choice to wizard_data
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"ollama"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/provider_credentials")
    body = resp.get_data(as_text=True)
    assert "No credentials needed" in body
    assert "ollama" in body


def test_provider_credentials_api_key_form_for_anthropic(client, app):
    """provider_credentials GET for anthropic renders an API-key form."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"anthropic"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/provider_credentials")
    body = resp.get_data(as_text=True)
    assert 'type="password"' in body
    assert 'name="api_key"' in body


def test_resume_upload_rejects_non_pdf_docx(client):
    """POST /onboarding/resume_upload with .exe extension returns 200 + error message (T-42-03)."""
    data = {"resume": (io.BytesIO(b"fake exe data"), "malicious.exe")}
    resp = client.post("/onboarding/resume_upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200  # re-renders with error, not 302
    body = resp.get_data(as_text=True)
    assert "Only .pdf and .docx files are supported" in body


def test_resume_upload_skip_advances_without_file(client):
    """POST /onboarding/resume_upload with skip=1 → 302 to profile_edit, no file required."""
    resp = client.post("/onboarding/resume_upload", data={"skip": "1"})
    assert resp.status_code == 302
    assert "/onboarding/profile_edit" in resp.headers["Location"]


def test_resume_upload_unlinks_temp_file(client, monkeypatch):
    """POST /onboarding/resume_upload calls os.unlink in the finally block regardless of parse outcome (T-42-07)."""
    unlinked = []
    original_unlink = __import__("os").unlink

    def tracking_unlink(p):
        unlinked.append(str(p))
        return original_unlink(p)

    monkeypatch.setattr("job_finder.web.onboarding.blueprint.os.unlink", tracking_unlink)
    # Mock parse_resume to avoid needing real pdfplumber
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.resume_parser.parse_resume",
        lambda p: {"skills": ["python"]},
    )

    fake_pdf = b"%PDF-1.4 fake content"
    data = {"resume": (io.BytesIO(fake_pdf), "resume.pdf")}
    resp = client.post("/onboarding/resume_upload", data=data, content_type="multipart/form-data")

    assert resp.status_code == 302  # success → redirect
    assert len(unlinked) == 1, f"Expected exactly 1 unlink call, got {unlinked}"


def test_imap_credentials_renders_app_password_hand_holding(client):
    """UAT F3 (2026-05-21) + WP13 (2026-06): IMAP step must include the two
    Google links, current-UI App-Passwords steps, and the collapsible 2FA
    enrollment walkthrough so a user with no 2FA can complete the step
    without leaving the page or opening docs/SETUP.md."""
    resp = client.get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # The marker stays in the template (test_each_step_renders enforces this
    # too — re-asserting locally protects against accidental removal during
    # template churn).
    assert "WIZARD_STEP_IMAP_CREDENTIALS_MARKER" in body

    # The two Google links must appear as anchors with target="_blank".
    assert 'href="https://myaccount.google.com/security"' in body
    assert 'href="https://myaccount.google.com/apppasswords"' in body
    assert 'target="_blank"' in body
    # Anchor opener-noreferrer hardening — required by the design.
    assert 'rel="noopener"' in body

    # App-Passwords steps must match Google's current UI (single App-name
    # field + Create button; the pre-2024 Mail / Other-device dropdowns are
    # gone and their wording must NOT come back).
    assert "App name" in body
    assert "Create" in body
    assert "Job Cannon" in body
    assert "16-character" in body
    assert "Other (custom name)" not in body

    # WP13: 2FA enrollment walkthrough — collapsed <details> with the
    # enrollment deep link (not the generic /security page).
    assert "2-minute walkthrough" in body
    assert 'href="https://myaccount.google.com/signinoptions/two-step-verification"' in body
    assert "Google prompt" in body

    # Both <details> panels (walkthrough + "Why am I doing this?") must be
    # collapsed by default — no `open` attribute on any of them.
    assert "<details" in body
    assert "Why am I doing this?" in body
    assert "<details open" not in body and "<details  open" not in body


def test_imap_credentials_post_failure_rerenders_with_error(client, monkeypatch):
    """D-08: POST /onboarding/imap_credentials with bad creds → HTTP 200 + error + preserved email."""
    from job_finder.web.onboarding.imap_test import ImapTestResult

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.imap_test.check_imap",
        lambda **kwargs: ImapTestResult(
            ok=False, error_kind="auth", message="Authentication failed — check your app password"
        ),
    )

    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "user@gmail.com", "app_password": "wrong pass word here"},
    )
    # D-08: re-render same page with HTTP 200, NOT redirect
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Authentication failed" in body
    assert "user@gmail.com" in body  # preserved


def test_imap_credentials_post_success_redirects_to_schedule(client, monkeypatch, app):
    """POST /onboarding/imap_credentials with valid creds → 302 to /onboarding/schedule + wizard_data persisted."""
    from job_finder.web.onboarding.imap_test import ImapTestResult

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.imap_test.check_imap",
        lambda **kwargs: ImapTestResult(ok=True, error_kind=None, message="ok", folder_count=8),
    )

    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "user@gmail.com", "app_password": "good pass word here"},
    )
    assert resp.status_code == 302
    assert "/onboarding/schedule" in resp.headers["Location"]

    import json

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["imap"]["email"] == "user@gmail.com"
        assert data["imap"]["verified"] is True
    finally:
        conn.close()


def test_imap_skip_button_bypasses_required_field_validation(client):
    resp = client.get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="skip"' in body
    assert "formnovalidate" in body


def test_imap_skip_persists_credentials_unverified(client, app):
    """D-08: skip-for-now saves creds but marks verified=False."""
    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "user@gmail.com", "app_password": "xxxx", "skip": "1"},
    )
    assert resp.status_code == 302
    assert "/onboarding/schedule" in resp.headers["Location"]

    import json

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["imap"]["verified"] is False
    finally:
        conn.close()


def test_schedule_post_writes_cadence_and_redirects_to_done(client, app):
    """POST /onboarding/schedule writes {schedule: {cadence_preset}}, redirects to /onboarding/done."""
    resp = client.post("/onboarding/schedule", data={"cadence_preset": "heavy"})
    assert resp.status_code == 302
    assert "/onboarding/done" in resp.headers["Location"]

    import json

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["schedule"]["cadence_preset"] == "heavy"
    finally:
        conn.close()


def test_schedule_invalid_preset_defaults_to_standard(client, app):
    """Unknown preset value gets sanitized to 'standard' (defensive — should never happen from real form)."""
    resp = client.post("/onboarding/schedule", data={"cadence_preset": "evil_value"})
    assert resp.status_code == 302

    import json

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["schedule"]["cadence_preset"] == "standard"
    finally:
        conn.close()


def test_done_get_renders_summary(client, app):
    """GET /onboarding/done renders the review summary card with wizard_data values."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"ollama"},"imap":{"email":"x@y.com"},"schedule":{"cadence_preset":"light"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ollama" in body
    assert "x@y.com" in body
    assert "light" in body


def test_done_page_includes_alert_setup_sections(client):
    """Stage 1 (NO-KEY-COMPENSATION-PLAN.md): /onboarding/done shows three
    collapsed <details> blocks (LinkedIn / Glassdoor / Indeed) with the
    matching sender email, an explicit link to the alert-setup page, and an
    honest disclaimer for Indeed.

    Static HTML, so the test is a substring/structural check — no behaviour
    change to assert.
    """
    resp = client.get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Existing summary marker still renders (defensive against template
    # restructure during the Stage 1 edit).
    assert "WIZARD_STEP_DONE_MARKER" in body

    # ---- LinkedIn block ----
    assert "Set up LinkedIn job alerts" in body
    assert "https://www.linkedin.com/jobs/job-alerts/" in body
    assert "jobalerts-noreply@linkedin.com" in body

    # ---- Glassdoor block ----
    assert "Set up Glassdoor job alerts" in body
    assert "glassdoor.com" in body
    assert "noreply@glassdoor.com" in body

    # ---- Indeed block + honest disclaimer ----
    assert "Set up Indeed job alerts" in body
    assert "indeed.com" in body
    assert "alert@indeed.com" in body
    # Spec calls for an honest note on Indeed reliability.
    assert "throttle" in body.lower() or "delay" in body.lower()

    # Anchor hardening: every outbound link opens in a new tab WITHOUT
    # passing window.opener and WITHOUT leaking the referrer (matches the
    # existing IMAP page convention enforced by test_imap_credentials_*).
    assert 'target="_blank"' in body
    assert 'rel="noopener"' in body

    # Sections are <details> elements and they are COLLAPSED by default.
    # The IMAP page also uses <details>, so substring counts are flaky;
    # we assert that at LEAST 3 <details> appear (one per provider).
    assert body.count("<details") >= 3
    # None of the new sections should be open by default.
    assert "<details open" not in body
    assert "<details  open" not in body


def test_done_post_redirects_to_jobs(client):
    """Done POST handler (plan 42-06) returns 302 redirect to /jobs with flash banner."""
    # Seed minimal wizard_data so done handler doesn't fail
    import sqlite3

    conn = sqlite3.connect(client.application.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"ollama"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post("/onboarding/done")
    # Plan 42-06 replaced the 501 stub with full atomic-finish implementation
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]
