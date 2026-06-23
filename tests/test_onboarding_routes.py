"""Integration tests for the onboarding blueprint routes (STRANGE-WIZ-02 + STRANGE-WIZ-05).

Parametrized GET smoke covers the 6 wizard-chain routes (success criterion 2 — each
renders with a unique marker string). The Settings-only /schedule route (folded out of
the chain in Issue #442) is covered separately. Targeted tests cover resume upload
security (T-42-03/T-42-07), IMAP failure re-render (D-08), and wizard_data write
semantics (D-13).
"""

import io
import re
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


# The 6 wizard-chain routes + their marker strings (success criterion 2). Issue
# #441 merged provider_credentials into provider_select; Issue #442 folded
# schedule out of the chain (it's now a Settings-only route covered by
# test_schedule_route_is_navigable_without_step_indicator below).
_ROUTE_TO_MARKER = [
    ("/onboarding/welcome", "WIZARD_STEP_WELCOME_MARKER"),
    ("/onboarding/provider_select", "WIZARD_STEP_PROVIDER_SELECT_MARKER"),
    ("/onboarding/resume_upload", "WIZARD_STEP_RESUME_UPLOAD_MARKER"),
    ("/onboarding/profile_edit", "WIZARD_STEP_PROFILE_EDIT_MARKER"),
    ("/onboarding/imap_credentials", "WIZARD_STEP_IMAP_CREDENTIALS_MARKER"),
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
    """Step indicator passes step_num and step_label to _base.html.

    Issue #441 collapsed provider_credentials into provider_select (8→7); Issue
    #442 folded schedule out of the wizard chain (7→6), so done is now the final
    step 6 of 6 and the schedule screen carries no indicator at all.
    """
    resp = client.get("/onboarding/welcome")
    body = resp.get_data(as_text=True)
    assert "Step 1 of 6" in body
    assert "Welcome" in body

    resp = client.get("/onboarding/done")
    body = resp.get_data(as_text=True)
    assert "Step 6 of 6" in body


def test_schedule_route_is_navigable_without_step_indicator(client):
    """Issue #442: /schedule stays reachable (Settings-only) but is no longer a
    wizard step — it returns 200 with its marker and NO 'Step N of' indicator."""
    resp = client.get("/onboarding/schedule")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "WIZARD_STEP_SCHEDULE_MARKER" in body
    # The rendered indicator (`Step N of M`) is omitted when no step context is
    # passed (see onboarding/_base.html). A static HTML comment "Step indicator"
    # always remains, so match the live "Step <n> of <m>" text specifically.
    assert re.search(r"Step \d+ of \d+", body) is None


def test_welcome_post_redirects_to_provider_select(client):
    """POST /onboarding/welcome → 302 to /onboarding/provider_select."""
    resp = client.post("/onboarding/welcome", data={})
    assert resp.status_code == 302
    assert "/onboarding/provider_select" in resp.headers["Location"]


def test_provider_select_post_no_cred_provider_writes_name_and_redirects(client, app):
    """POST a no-cred provider writes {provider: {name}} and redirects to resume_upload (Issue #441).

    No credential round-trip — the merged screen sends $0 CLIs straight through.
    """
    resp = client.post("/onboarding/provider_select", data={"provider_name": "ollama"})
    assert resp.status_code == 302
    assert "/onboarding/resume_upload" in resp.headers["Location"]

    # Verify wizard_data was written (name only, no api_key)
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        assert row is not None
        import json

        data = json.loads(row[0])
        assert data["provider"]["name"] == "ollama"
        assert "api_key" not in data["provider"]
    finally:
        conn.close()


def test_provider_select_post_byo_writes_api_key_and_redirects(client, app):
    """Issue #441: POST a BYO-key provider with an api_key writes both and redirects to resume_upload."""
    resp = client.post(
        "/onboarding/provider_select",
        data={"provider_name": "anthropic", "api_key": "sk-test-123"},
    )
    assert resp.status_code == 302
    assert "/onboarding/resume_upload" in resp.headers["Location"]

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        import json

        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["provider"]["name"] == "anthropic"
        assert data["provider"]["api_key"] == "sk-test-123"
    finally:
        conn.close()


def test_provider_select_post_byo_empty_key_flashes_and_does_not_advance(client, app):
    """Issue #441: a BYO-key provider with an empty api_key re-renders with the
    'API key is required' flash and does NOT advance (mirrors old provider_credentials)."""
    resp = client.post(
        "/onboarding/provider_select",
        data={"provider_name": "anthropic", "api_key": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "API key is required" in body
    # No provider was committed.
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        import json

        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert "provider" not in data
    finally:
        conn.close()


def test_provider_credentials_route_removed(client):
    """Issue #441: the standalone provider_credentials route no longer resolves (404)."""
    assert client.get("/onboarding/provider_credentials").status_code == 404
    assert client.post("/onboarding/provider_credentials").status_code == 404


def test_provider_select_renders_inline_api_key_for_byo(client, app):
    """Issue #441: the merged screen renders the inline API-key field for a BYO-key provider.

    With no CLIs detected (autouse mock returns []), the screen offers the
    Anthropic BYO-key path with its credential field folded inline.
    """
    resp = client.get("/onboarding/provider_select")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'type="password"' in body
    assert 'name="api_key"' in body


def test_provider_select_no_api_key_field_for_no_cred(client):
    """Issue #441: a no-cred provider selection renders no inline API-key field."""
    from unittest.mock import patch as _patch

    from job_finder.web.providers.detection import ProviderHandle

    handle = ProviderHandle(
        name="ollama", binary_path="/usr/bin/ollama", cost_label="$0", priority=3
    )
    with _patch(
        "job_finder.web.onboarding.blueprint.detect_available_providers",
        return_value=[handle],
    ):
        resp = client.get("/onboarding/provider_select")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'type="password"' not in body
    assert 'name="api_key"' not in body


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


def test_resume_parse_failure_surfaces_notice_on_profile_edit(client, monkeypatch):
    """Issue #397: a resume upload whose parse yields no skills must not be swallowed.

    Uploading a file the parser can't read writes resume_parse_failed=True to wizard
    data; the subsequent profile_edit GET surfaces a non-blocking notice instead of
    rendering an empty skills field with no explanation.
    """

    def boom(p, conn=None, config=None):
        raise RuntimeError("unreadable resume")

    monkeypatch.setattr("job_finder.web.onboarding.blueprint.resume_parser.parse_resume", boom)

    data = {"resume": (io.BytesIO(b"%PDF-1.4 fake content"), "resume.pdf")}
    resp = client.post("/onboarding/resume_upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302  # parse failure still advances (non-blocking)

    page = client.get("/onboarding/profile_edit")
    body = page.get_data(as_text=True)
    assert "read any skills from your resume" in body


def test_resume_skip_shows_no_parse_notice(client):
    """Issue #397: skipping the resume is a deliberate choice, not a failure —
    profile_edit must NOT show the parse-failure notice."""
    resp = client.post("/onboarding/resume_upload", data={"skip": "1"})
    assert resp.status_code == 302

    page = client.get("/onboarding/profile_edit")
    body = page.get_data(as_text=True)
    assert "read any skills from your resume" not in body


def test_resume_parse_success_shows_no_notice_and_prefills_skills(client, monkeypatch):
    """A successful parse prefills the skills field and shows no failure notice."""
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.resume_parser.parse_resume",
        lambda p, conn=None, config=None: {"skills": ["python", "sql"]},
    )

    data = {"resume": (io.BytesIO(b"%PDF-1.4 fake content"), "resume.pdf")}
    resp = client.post("/onboarding/resume_upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302

    page = client.get("/onboarding/profile_edit")
    body = page.get_data(as_text=True)
    assert "read any skills from your resume" not in body
    assert "python" in body
    assert "sql" in body


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


def test_imap_credentials_surfaces_readonly_and_workspace_tradeoffs(client):
    """Issue #443: the Gmail/IMAP step must set expectations before the user
    generates credentials — the connection is read-only/fetch-only (never marks
    mail read, labels it, or changes the inbox), and Workspace users whose admin
    has disabled IMAP/app passwords are pointed at the existing Skip-for-now path."""
    resp = client.get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Read-only / fetch-only expectation.
    assert "Read-only / fetch-only" in body
    assert "never marks them read, labels them, or changes your inbox" in body

    # Workspace-admin fallback pointing at Skip for now.
    assert "Google Workspace account" in body
    assert "disabled IMAP or app passwords" in body
    assert "Skip for now" in body

    # The new copy must not reopen the "collapsed by default" invariant —
    # it adds no <details open> block.
    assert "<details open" not in body and "<details  open" not in body


def test_imap_credentials_post_failure_rerenders_with_error(client, monkeypatch):
    """D-08: POST /onboarding/imap_credentials with bad creds → HTTP 200 + error + preserved email.

    Bug #9: the submitted app password must also be echoed back into the form so a
    retry doesn't force the user to re-type the full 16-char password.
    """
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
    # Bug #9: the typed app password is preserved in the re-rendered field value.
    assert 'value="wrong pass word here"' in body


def test_imap_credentials_post_missing_field_preserves_password(client):
    """Bug #9: a missing-email submission still echoes the typed app password back.

    The "Both Gmail address and app password are required" branch re-renders before
    any smoke test runs, so it exercises the password echo-back independently.
    """
    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "", "app_password": "typed pass word here"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Both Gmail address and app password are required" in body
    assert 'value="typed pass word here"' in body


def test_imap_credentials_post_success_redirects_to_done(client, monkeypatch, app):
    """Issue #442: POST /onboarding/imap_credentials with valid creds → 302 to
    /onboarding/done (the schedule step was folded out of the chain) + wizard_data persisted."""
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
    assert "/onboarding/done" in resp.headers["Location"]

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
    """D-08: skip-for-now saves creds but marks verified=False.

    Issue #442: skip now advances to /onboarding/done (schedule folded out)."""
    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "user@gmail.com", "app_password": "xxxx", "skip": "1"},
    )
    assert resp.status_code == 302
    assert "/onboarding/done" in resp.headers["Location"]

    import json

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        data = json.loads(row[0])
        assert data["imap"]["verified"] is False
    finally:
        conn.close()


def test_imap_credentials_field_has_autocomplete_and_pattern(client):
    """Issue #399: Gmail field advertises autocomplete=email + an HTML pattern."""
    resp = client.get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'autocomplete="email"' in body
    assert 'inputmode="email"' in body
    assert "pattern=" in body


def test_imap_credentials_prefills_email_from_resume(client, app):
    """Issue #399: GET prefills the Gmail field from the parsed-resume email."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET "
            'wizard_data=\'{"resume_profile":{"email":"jane.doe@gmail.com"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="jane.doe@gmail.com"' in body


def test_imap_credentials_existing_email_wins_over_resume(client, app):
    """An email already entered on the IMAP step takes precedence over the resume one."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET "
            'wizard_data=\'{"imap":{"email":"chosen@gmail.com"},'
            '"resume_profile":{"email":"resume@gmail.com"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/imap_credentials")
    body = resp.get_data(as_text=True)
    assert 'value="chosen@gmail.com"' in body
    assert "resume@gmail.com" not in body


def test_imap_credentials_rejects_malformed_email(client, monkeypatch):
    """Issue #399: a malformed address is rejected before the IMAP smoke test."""
    called = False

    def _fail_if_called(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("check_imap must not run for an invalid address")

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.imap_test.check_imap",
        _fail_if_called,
    )

    resp = client.post(
        "/onboarding/imap_credentials",
        data={"email": "not-an-email", "app_password": "good pass word here"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "valid email" in body
    assert "not-an-email" in body  # preserved for correction
    assert called is False


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


def test_done_get_renders_editable_cadence_defaulting_to_standard(client):
    """Issue #442: with no prior schedule choice, the done review screen renders
    an inline editable cadence control with `standard` pre-selected."""
    resp = client.get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The editable control is a radio group named cadence_preset, inside the form.
    assert 'name="cadence_preset"' in body
    # standard is the checked default; light/heavy are not.
    assert 'value="standard" class="mt-1" checked' in body
    assert 'value="light" class="mt-1" checked' not in body
    assert 'value="heavy" class="mt-1" checked' not in body


def test_done_get_preselects_prior_cadence_choice(client, app):
    """Issue #442: a cadence already stashed in wizard_data (e.g. the user visited
    the Settings-only schedule route) is pre-selected on the done review screen."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"schedule":{"cadence_preset":"heavy"}}\' '
            "WHERE id=1"
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="heavy" class="mt-1" checked' in body
    assert 'value="standard" class="mt-1" checked' not in body


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
    from unittest.mock import MagicMock, patch

    conn = sqlite3.connect(client.application.config["DB_PATH"])
    try:
        conn.execute(
            'UPDATE onboarding_state SET wizard_data=\'{"provider":{"name":"ollama"}}\' WHERE id=1'
        )
        conn.commit()
    finally:
        conn.close()

    # POST /done schedules a real date+5s wizard_first_ingest job (D-17). The app
    # fixture runs a real BackgroundScheduler, so WITHOUT this mock the job lands on
    # the shared scheduler and fires inside an unrelated later test, writing the
    # developer's repo jobs.db (test-isolation leak). Every other /done test mocks
    # get_scheduler the same way; this one was the lone omission.
    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = client.post("/onboarding/done")
    # Plan 42-06 replaced the 501 stub with full atomic-finish implementation
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Issue #398 — profile_edit autofill-confirm: target_titles prefill + chips
# ---------------------------------------------------------------------------
_PARSE_RESUME = "job_finder.web.onboarding.blueprint.resume_parser.parse_resume"


def _upload_resume(client):
    """Drive a resume_upload POST so the mocked parser output lands in wizard_data."""
    data = {"resume": (io.BytesIO(b"%PDF-1.4 fake content"), "resume.pdf")}
    return client.post("/onboarding/resume_upload", data=data, content_type="multipart/form-data")


def test_profile_edit_prefills_target_titles_from_suggested(client, monkeypatch):
    """Issue #398 (a): parser-suggested titles prefill the field as suggested chips.

    A resume parse returning target_roles_suggested=["Staff Engineer"] with no prior
    user edit renders the title as a chip carrying the "suggested" class marker, and
    the hidden target_titles textarea (the POST contract) is seeded with the value.
    """
    monkeypatch.setattr(
        _PARSE_RESUME,
        lambda p, conn=None, config=None: {
            "skills": ["python"],
            "target_roles_suggested": ["Staff Engineer"],
        },
    )
    assert _upload_resume(client).status_code == 302

    body = client.get("/onboarding/profile_edit").get_data(as_text=True)
    assert "Staff Engineer" in body
    assert "onb-chip-suggested" in body  # suggested visual marker
    # Hidden textarea preserves the name="target_titles" POST contract + seeded value.
    assert re.search(r'name="target_titles"[^>]*>Staff Engineer</textarea>', body), (
        "hidden target_titles textarea not seeded with the suggested value"
    )


def test_profile_edit_user_titles_override_suggested(client, monkeypatch):
    """Issue #398 (b): a user-entered target_titles value wins over the suggestion."""
    monkeypatch.setattr(
        _PARSE_RESUME,
        lambda p, conn=None, config=None: {
            "skills": ["python"],
            "target_roles_suggested": ["Staff Engineer"],
        },
    )
    assert _upload_resume(client).status_code == 302

    # User confirms their own title — valid POST persists the profile_edit slice.
    resp = client.post(
        "/onboarding/profile_edit",
        data={"target_titles": "My Own Title", "skills": "python"},
    )
    assert resp.status_code == 302

    body = client.get("/onboarding/profile_edit").get_data(as_text=True)
    assert "My Own Title" in body
    assert "Staff Engineer" not in body  # suggestion does NOT override the user value


@pytest.mark.parametrize(
    "parsed",
    [
        {"skills": ["python"]},  # key absent
        {"skills": ["python"], "target_roles_suggested": []},  # empty list
        {"skills": ["python"], "target_roles_suggested": "not-a-list"},  # non-list
    ],
)
def test_profile_edit_handles_missing_target_roles_suggested(client, monkeypatch, parsed):
    """Issue #398 (c): absent/empty/non-list target_roles_suggested renders empty, no crash."""
    monkeypatch.setattr(_PARSE_RESUME, lambda p, conn=None, config=None: parsed)
    assert _upload_resume(client).status_code == 302

    page = client.get("/onboarding/profile_edit")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    # Empty hidden textarea — no None text, no leftover suggestion content.
    assert '<textarea name="target_titles" class="chips-hidden hidden"></textarea>' in body


def test_profile_edit_chip_serialization_roundtrips(client):
    """Issue #398 (d): a POST with the hidden-textarea contents redirects and persists
    the raw newline string that the done-step _split_lines turns into the split list."""
    import json as _json

    resp = client.post(
        "/onboarding/profile_edit",
        data={
            "target_titles": "Title A\nTitle B",
            "target_locations": "Remote",
            "skills": "go\nrust",
            "min_salary": "120000",
        },
    )
    assert resp.status_code == 302
    assert "/onboarding/imap_credentials" in resp.headers["Location"]

    conn = sqlite3.connect(client.application.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
    finally:
        conn.close()
    persisted = _json.loads(row[0])["profile_edit"]
    assert persisted["target_titles"] == "Title A\nTitle B"
    # Mirror the done-step _split_lines contract (blueprint.py:633-634): the persisted
    # newline string splits into the target_titles list written to config/profile.
    split = [ln.strip() for ln in persisted["target_titles"].splitlines() if ln.strip()]
    assert split == ["Title A", "Title B"]


def test_done_post_preserves_existing_curated_target_titles(client):
    """Re-running onboarding must NOT clobber a curated target_titles/skills list
    with the wizard's (often empty/example-prefilled) field — the 2026-06-18
    27->2 wipe class. Existing non-empty lists are preserved (fill-only-when-
    absent); only a fresh install with no curated list takes the wizard values."""
    import json
    import sqlite3
    from unittest.mock import MagicMock, patch

    import yaml

    from job_finder.web import user_data_dirs

    curated = [f"Title {i}" for i in range(27)]
    cfg_path = user_data_dirs.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"profile": {"target_titles": list(curated), "skills": ["Python"]}}, f)

    # Wizard would write only the 2 example defaults (the clobber attempt).
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET wizard_data=? WHERE id=1",
            (
                json.dumps(
                    {
                        "provider": {"name": "ollama"},
                        "profile_edit": {
                            "target_titles": "Data Scientist\nSenior Data Scientist",
                            "skills": "",
                        },
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = client.post("/onboarding/done")
    assert resp.status_code == 302

    with open(cfg_path, encoding="utf-8") as f:
        written = yaml.safe_load(f)
    assert written["profile"]["target_titles"] == curated  # preserved, not clobbered
    assert written["profile"]["skills"] == ["Python"]
