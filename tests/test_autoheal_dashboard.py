"""Tests for the dashboard degraded-sources widget (autoheal Phase A, Task 8)."""

from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm


def _degrade(conn, source="glassdoor"):
    """Drive *source* to DEGRADED state by recording baseline then consecutive breaks."""
    # Establish a positive baseline (≥1 mean yield required for break rule to fire)
    for _ in range(3):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=2)
    # Record BREAK_THRESHOLD consecutive zero-yield extractions → status upgrades to DEGRADED
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=0)


def test_context_builder_lists_degraded(app):
    from job_finder.web.blueprints.dashboard import _get_degraded_sources_context
    from job_finder.web.db_helpers import get_db

    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
        ctx = _get_degraded_sources_context(conn)
    assert any(d["source"] == "glassdoor" for d in ctx["degraded"])


def test_dashboard_shows_degraded_widget(client, app):
    from job_finder.web.db_helpers import get_db

    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "glassdoor" in resp.data.decode()


def test_degraded_fragment_requires_htmx(client, app):
    from job_finder.web.db_helpers import get_db

    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
    resp = client.get("/dashboard/degraded-sources", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<html" not in body and "<!DOCTYPE" not in body


def test_degraded_fragment_redirect_without_htmx(client):
    resp = client.get("/dashboard/degraded-sources")
    # Non-HTMX request should redirect to the dashboard index
    assert resp.status_code in (301, 302)


def test_healthy_system_shows_empty_state(client):
    resp = client.get("/dashboard/degraded-sources", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "All sources healthy" in resp.data.decode()


# ---------------------------------------------------------------------------
# Heal Activity panel (Phase D / D5)
# ---------------------------------------------------------------------------


def _audit(conn, source, outcome):
    from job_finder.web.autoheal.audit import record_audit

    record_audit(conn, source, "email", outcome)


def test_dashboard_renders_heal_panel_empty(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Heal Activity" in body
    assert "No heal activity yet" in body


def test_heal_panel_shows_audit_rows_excluding_no_provider(client, app):
    from job_finder.web.db_helpers import get_db

    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _audit(conn, "linkedin", "adopted")
        _audit(conn, "glassdoor", "no_provider")

    resp = client.get("/dashboard/")
    body = resp.data.decode()
    assert "adopted" in body
    assert "no_provider" not in body


def test_heal_panel_renders_pending_bundle(client, app, tmp_path, monkeypatch):
    from job_finder.web.autoheal import upstream_reporter as ur

    # The conftest autouse fixture isolates JOB_CANNON_USER_DATA_DIR; write a
    # bundle into THAT root so pending_bundles (production path) finds it.
    ur.write_bundle(
        {
            "schema_version": 1,
            "source": "careers:acme.com",
            "surface": "careers",
            "recipe": {"container_selector": "li"},
            "failing_sample": "<html>" + "x" * 3000 + "</html>",
            "drift": {"consecutive_breaks": 3},
            "created_at": "2026-06-10T21:30:00",
            "app_version": "v5.0.0",
        }
    )

    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Pending contributions" in body
    assert "careers:acme.com" in body
    assert "PII-scrubbed sample" in body  # consent text
    assert "Copy bundle" in body

    # Issue link: present, urlencoded, bounded.
    import re as _re

    m = _re.search(r'href="(https://github\.com/[^"]+/issues/new\?[^"]+)"', body)
    assert m, "issue link missing"
    url = m.group(1).replace("&amp;", "&")
    assert "title=" in url and "body=" in url
    assert len(url) <= 8_000
    assert " " not in url  # urlencoded


def test_bundle_issue_url_caps_total_length():
    from job_finder.web.blueprints.dashboard import _bundle_issue_url

    bundle = {
        "source": "careers:acme.com",
        "surface": "careers",
        "recipe": {"container_selector": "li.opening"},
        "failing_sample": "<div class='x'>&" * 5_000,  # quote-expansion-heavy
        "drift": {},
        "app_version": "v5.0.0",
    }
    url = _bundle_issue_url("Senkichi/job-cannon", bundle)
    assert len(url) <= 8_000
