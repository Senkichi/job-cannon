"""/admin/review triage routes (Phase 47.07).

Covers the GET listing (full-page vs HTMX fragment) and the approve/drop POST
actions. Approve clears unresolved_reasons (and per-location unresolved flags);
drop sets pipeline_status='rejected'. Both return ('', 200) so HTMX swaps the
row out (empty body → row disappears), per the CLAUDE.md "never 204" rule.
"""

from __future__ import annotations

import json
import sqlite3


def _seed_unresolved(
    db_path: str,
    *,
    key: str = "acmeco|staff-eng",
    reasons: str = '["title_metadata_blob"]',
    locations: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, "
            "last_seen, pipeline_status, unresolved_reasons, locations_structured) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                "Staff Engineer",
                "AcmeCo",
                "Remote",
                "2026-01-01",
                "2026-01-01",
                "new",
                reasons,
                locations,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read(db_path: str, key: str, col: str):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(f"SELECT {col} FROM jobs WHERE dedup_key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_review_lists_unresolved_row(client, tmp_db_path):
    _seed_unresolved(tmp_db_path)
    resp = client.get("/admin/review")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AcmeCo" in body
    assert "title_metadata_blob" in body


def test_review_excludes_resolved_rows(client, tmp_db_path):
    # A clean row ('[]' reasons, no unresolved location) must not appear.
    _seed_unresolved(tmp_db_path, key="cleanco|eng", reasons="[]", locations=None)
    body = client.get("/admin/review").get_data(as_text=True)
    assert "cleanco" not in body.lower()


def test_direct_get_returns_full_page(client, tmp_db_path):
    _seed_unresolved(tmp_db_path)
    body = client.get("/admin/review").get_data(as_text=True)
    assert "<html" in body.lower()  # base.html wrapper


def test_htmx_get_returns_fragment(client, tmp_db_path):
    _seed_unresolved(tmp_db_path)
    body = client.get("/admin/review", headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "<html" not in body.lower()
    assert "AcmeCo" in body  # the row itself is still rendered


def test_approve_clears_reasons(client, tmp_db_path):
    _seed_unresolved(tmp_db_path, key="appr|co")
    resp = client.post("/admin/review/appr|co/approve")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == ""
    assert _read(tmp_db_path, "appr|co", "unresolved_reasons") == "[]"


def test_approve_clears_location_unresolved_flag(client, tmp_db_path):
    _seed_unresolved(
        tmp_db_path,
        key="locappr|co",
        reasons="[]",
        locations='[{"city": "NYC", "unresolved": true}]',
    )
    client.post("/admin/review/locappr|co/approve")
    locs = json.loads(_read(tmp_db_path, "locappr|co", "locations_structured"))
    assert all(loc["unresolved"] is False for loc in locs)


def test_approve_appends_audit_note(client, tmp_db_path):
    _seed_unresolved(tmp_db_path, key="note|co")
    client.post("/admin/review/note|co/approve")
    notes = _read(tmp_db_path, "note|co", "notes") or ""
    assert "approved" in notes
    assert "title_metadata_blob" in notes


def test_drop_sets_rejected(client, tmp_db_path):
    _seed_unresolved(tmp_db_path, key="drop|co")
    resp = client.post("/admin/review/drop|co/drop")
    assert resp.status_code == 200
    assert _read(tmp_db_path, "drop|co", "pipeline_status") == "rejected"


def test_approve_missing_row_404(client, tmp_db_path):
    resp = client.post("/admin/review/nope|missing/approve")
    assert resp.status_code == 404
