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


# ---------------------------------------------------------------------------
# P1.6 — salary_implausible quarantine surfaces + the enrichment loop closes
# ---------------------------------------------------------------------------


def test_review_renders_salary_implausible_badge(client, tmp_db_path):
    """The arbitrary-code badge renderer surfaces the new salary_implausible reason."""
    _seed_unresolved(tmp_db_path, key="quar|co", reasons='["salary_implausible"]')
    body = client.get("/admin/review").get_data(as_text=True)
    assert "quar" in body  # the row rendered
    assert "salary_implausible" in body  # its reason badge rendered verbatim


def test_salary_implausible_quarantine_loop(client, tmp_db_path):
    """End-to-end P1.6 loop (the quarantine → enrichment re-entry → clear cycle):

    1. A junk feed salary ingests through the real capture path → canonical NULL,
       salary_implausible set, evidence retained.
    2. The row surfaces on /admin/review.
    3. The enrichment selection clause (salary_min IS NULL) picks it.
    4. A later enrichment pass writes a plausible pair (stand-in for the LLM result).
    5. The canonical pair is written and salary_implausible is surgically cleared.
    6. The row leaves /admin/review.
    """
    from job_finder.db import upsert_job
    from job_finder.models import Job
    from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob
    from job_finder.salary_normalizer import SalaryObservation, salary_capture_fields
    from job_finder.web.data_enricher import _persist
    from job_finder.web.location_canonical import JobLocation

    # A fully-resolved location so salary_implausible is the ONLY review reason —
    # the row must leave /admin/review once the salary is cleared.
    loc = JobLocation(
        city="New York",
        region=None,
        region_code=None,
        country="United States",
        country_code="US",
        workplace_type="ONSITE",
        raw="New York, NY",
        unresolved=False,
    )

    # --- ingest junk salary ($46 feed value → implausible) via the real capture path
    obs = SalaryObservation(
        min_value=46.0, max_value=None, period="unknown", provenance="feed_string"
    )
    job = Job(
        title="Staff Data Scientist",
        company="LoopTest Co",
        location="New York, NY",
        source="portal_jooble",
        source_url="https://example.com/loop-ds",
        source_id="",
        description="x" * 300,
        **salary_capture_fields(obs),
    )
    # Salary observation + provenance ride on the Job via salary_capture_fields;
    # source_meta only supplies the (resolved) location so I-07 is satisfied.
    parsed = ParsedJob.from_job(
        job,
        source_meta={
            "locations_raw": ["New York, NY"],
            "locations_structured": [loc],
        },
    )
    assert isinstance(parsed, UnresolvedParsedJob)
    assert "salary_implausible" in parsed.unresolved_reasons
    dedup_key = parsed.dedup_key

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        upsert_job(conn, parsed)

        # (1) canonical NULL + reason persisted + evidence retained
        row = conn.execute(
            "SELECT salary_min, salary_max, unresolved_reasons, salary_observations "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row["salary_min"] is None and row["salary_max"] is None
        assert "salary_implausible" in json.loads(row["unresolved_reasons"])
        assert len(json.loads(row["salary_observations"])) == 1

        # (2) surfaces on /admin/review
        body = client.get("/admin/review").get_data(as_text=True)
        assert "LoopTest Co" in body
        assert "salary_implausible" in body

        # (3) enrichment selection (salary_min IS NULL) picks the row
        picked = conn.execute(
            "SELECT 1 FROM jobs WHERE salary_min IS NULL AND dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert picked is not None

        # (4) a later enrichment pass resolves a plausible pair (LLM stand-in)
        job_row = dict(
            conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
        )
        _persist(
            conn,
            job_row,
            {"salary_min": 170_000, "salary_max": 200_000, "salary_provenance": "llm_extract"},
            "low",
        )

        # (5) canonical written + reason surgically cleared
        row2 = conn.execute(
            "SELECT salary_min, salary_max, unresolved_reasons FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert (row2["salary_min"], row2["salary_max"]) == (170_000, 200_000)
        assert json.loads(row2["unresolved_reasons"]) == []
    finally:
        conn.close()

    # (6) the row has left /admin/review
    body2 = client.get("/admin/review").get_data(as_text=True)
    assert "LoopTest Co" not in body2


def test_persist_preserves_other_reasons_when_clearing_salary(client, tmp_db_path):
    """Clearing salary_implausible is surgical — an unrelated reason is preserved."""
    from job_finder.web.data_enricher import _persist

    _seed_unresolved(
        tmp_db_path,
        key="multi|co",
        reasons='["salary_implausible", "jd_full_junk"]',
    )
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        job_row = dict(
            conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", ("multi|co",)).fetchone()
        )
        _persist(
            conn,
            job_row,
            {"salary_min": 150_000, "salary_max": 190_000, "salary_provenance": "llm_extract"},
            "low",
        )
        reasons = json.loads(
            conn.execute(
                "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", ("multi|co",)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert "salary_implausible" not in reasons  # cleared
    assert "jd_full_junk" in reasons  # preserved
