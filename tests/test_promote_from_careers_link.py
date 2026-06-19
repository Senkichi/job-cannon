"""Tests for promote_from_careers_link — the #453 careers-page promotion seam.

Asserts it reuses the shared verify + m076 collision-guarded UPDATE writer:
a miss company is promoted to 'hit' with a careers_link evidence trigger, a
slug already owned by another company is refused, and an already-'hit' company
is a no-op.
"""

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from job_finder.web.ats_identity_reconcile import promote_from_careers_link


@pytest.fixture()
def seeded_company(tmp_db_path):
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_probe_status, scan_enabled,
           created_at, updated_at)
           VALUES ('customco', 'CustomCo', 'miss', 1, ?, ?)""",
        (now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return tmp_db_path, int(cid)


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
def test_promotes_with_careers_link_trigger(mock_verify, seeded_company):
    path, cid = seeded_company
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    res = promote_from_careers_link(
        conn,
        cid,
        "greenhouse",
        "customco",
        page_url="https://customco.com/careers",
        config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
    )
    conn.close()
    assert res["outcome"] == "promoted"
    assert res["platform"] == "greenhouse"
    assert res["page_url"] == "https://customco.com/careers"

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "customco"
    assert row["ats_evidence_trigger"].startswith("careers_link:")
    assert "customco.com/careers" in row["ats_evidence_trigger"]


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
def test_refuses_on_slug_collision(mock_verify, seeded_company):
    path, cid = seeded_company
    now = datetime.now().isoformat()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Pre-existing owner of (greenhouse, customco).
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_slug, ats_probe_status,
               scan_enabled, created_at, updated_at)
           VALUES ('Owner', 'Owner', 'greenhouse', 'customco', 'hit', 1, ?, ?)""",
        (now, now),
    )
    owner_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    res = promote_from_careers_link(
        conn,
        cid,
        "greenhouse",
        "customco",
        page_url="https://customco.com/careers",
        config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
    )
    assert res["outcome"] == "slug_collision"
    assert res["existing_owner_id"] == owner_id

    # Loser untouched; owner keeps the pair.
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    assert row["ats_platform"] is None
    assert row["ats_probe_status"] == "miss"
    conn.close()


def test_skips_already_hit(seeded_company):
    path, cid = seeded_company
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE companies SET ats_probe_status='hit', ats_platform='lever', ats_slug='x' WHERE id=?",
        (cid,),
    )
    conn.commit()
    res = promote_from_careers_link(
        conn,
        cid,
        "greenhouse",
        "customco",
        page_url="https://customco.com/careers",
        config={},
    )
    conn.close()
    assert res["outcome"] == "skipped_already_hit"


def test_missing_company_returns_missing(seeded_company):
    path, _cid = seeded_company
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    res = promote_from_careers_link(
        conn,
        999999,
        "greenhouse",
        "customco",
        page_url="https://customco.com/careers",
        config={},
    )
    conn.close()
    assert res["outcome"] == "missing_company"
