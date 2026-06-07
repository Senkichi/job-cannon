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
