"""Tests for the Settings source-health banner (reliability epic #436).

Covers the m104 migration columns, the no-raise record/clear helpers, the
sources_needing_attention reader (credential vs degraded classification +
renewal links), and the Settings page + HTMX fragment surface.
"""

import sqlite3

from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.db_helpers import get_db

_THORDATA_EXPIRED = "Thordata account error: Package has expired!"


def _degrade(conn, source="glassdoor"):
    """Drive *source* to parser-DEGRADED via baseline + consecutive zero-yields."""
    for _ in range(3):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=2)
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=0)


# ---------------------------------------------------------------------------
# Migration + reader/writer unit tests
# ---------------------------------------------------------------------------


def test_m104_adds_last_error_columns(app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        cols = {row[1] for row in conn.execute("PRAGMA table_info(source_health)").fetchall()}
    assert "last_error" in cols
    assert "last_error_at" in cols


def test_record_credential_error_surfaces_with_renewal_link(app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        hm.record_source_error(conn, "thordata", _THORDATA_EXPIRED)
        rows = hm.sources_needing_attention(conn)
    row = next(r for r in rows if r["source"] == "thordata")
    assert row["kind"] == "credential"
    assert row["renewal_url"] == "https://console.thordata.com/"
    assert row["last_error"] == _THORDATA_EXPIRED


def test_parser_degraded_source_classified_degraded(app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
        rows = hm.sources_needing_attention(conn)
    row = next(r for r in rows if r["source"] == "glassdoor")
    assert row["kind"] == "degraded"


def test_clear_source_error_removes_from_attention(app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        hm.record_source_error(conn, "serpapi", "serpapi: API key rejected (HTTP 401)")
        assert any(r["source"] == "serpapi" for r in hm.sources_needing_attention(conn))
        hm.clear_source_error(conn, "serpapi")
        rows = hm.sources_needing_attention(conn)
    assert not any(r["source"] == "serpapi" for r in rows)


def test_record_and_clear_never_raise_on_bad_connection():
    conn = sqlite3.connect(":memory:")
    conn.close()  # any execute now raises ProgrammingError; helpers must swallow it
    hm.record_source_error(conn, "thordata", "boom")
    hm.clear_source_error(conn, "thordata")


# ---------------------------------------------------------------------------
# Settings page + HTMX fragment
# ---------------------------------------------------------------------------


def test_settings_page_shows_source_banner(client, app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        hm.record_source_error(conn, "thordata", _THORDATA_EXPIRED)
    resp = client.get("/settings/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "thordata" in body
    assert "console.thordata.com" in body


def test_source_health_fragment_requires_htmx(client, app):
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        hm.record_source_error(conn, "thordata", _THORDATA_EXPIRED)
    resp = client.get("/settings/source-health", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<html" not in body and "<!DOCTYPE" not in body
    assert "thordata" in body


def test_source_health_fragment_redirect_without_htmx(client):
    resp = client.get("/settings/source-health")
    assert resp.status_code in (301, 302)


def test_source_health_fragment_empty_state_renders_nothing(client):
    resp = client.get("/settings/source-health", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # Empty list -> the banner card is not rendered (only the wrapper div).
    assert "Source attention needed" not in resp.data.decode()
