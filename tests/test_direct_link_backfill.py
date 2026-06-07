"""Tests for the direct-link backfill function and admin route."""

from __future__ import annotations

import sqlite3

from job_finder.web.db_migrate import run_migrations


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_backfill_resolves_null_rows_and_is_idempotent(tmp_path):
    from job_finder.web.backfill_direct_links import backfill_direct_links

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, source_urls) "
        "VALUES ('a', 'DS', 'Acme', 'R', '2026-01-01', '2026-01-01', "
        "'[\"https://jobs.lever.co/acme/1\"]')"
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, source_urls) "
        "VALUES ('b', 'DS', 'Beta', 'R', '2026-01-01', '2026-01-01', "
        "'[\"https://www.linkedin.com/jobs/view/9\"]')"
    )
    conn.commit()

    summary = backfill_direct_links(conn, {})
    assert summary["resolved"] == 1
    assert summary["strict"] == 1

    a = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key='a'").fetchone()
    b = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key='b'").fetchone()
    assert a["direct_url"] == "https://jobs.lever.co/acme/1"
    assert b["direct_url"] is None

    summary2 = backfill_direct_links(conn, {})
    assert summary2["resolved"] == 0
    conn.close()


def test_admin_backfill_route_returns_summary(client, monkeypatch):
    """The admin route invokes the backfill and returns its summary JSON."""
    import job_finder.web.blueprints.admin as admin_mod

    captured = {}

    def fake_backfill(conn, config):
        captured["called"] = True
        return {"scanned": 3, "resolved": 2, "strict": 1, "loose": 1}

    monkeypatch.setattr(admin_mod, "backfill_direct_links", fake_backfill)

    resp = client.post("/admin/jobs/direct-links/backfill")
    assert resp.status_code == 200
    assert resp.get_json()["resolved"] == 2
    assert captured.get("called") is True
