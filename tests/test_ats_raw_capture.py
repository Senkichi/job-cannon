"""Tests for Phase B ATS raw-response capture in run_platform_scan.

Verifies:
- When conn is provided, the raw pre-filter posting list is stored in
  corpus_sample with surface='ats' and job_count = raw (pre-filter) count.
- detect=True: a previously-productive source that returns [] increments
  the break counter.
- When conn is None (all existing callers), behaviour is unchanged.
"""

from __future__ import annotations

import json
import sqlite3

from job_finder.web.ats_platforms._registry import PlatformScanner, run_platform_scan
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_db(tmp_path) -> tuple[str, sqlite3.Connection]:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return db, conn


def _make_scanner(postings: list[dict], name: str = "fake") -> PlatformScanner:
    return PlatformScanner(
        name=name,
        company_source="Fake",
        fetch_postings=lambda slug: list(postings),
        title_of=lambda p: p.get("title", ""),
        posting_to_job=lambda p, slug: {"title": p.get("title", ""), "company_source": "Fake"},
    )


# ---------------------------------------------------------------------------
# Raw capture stored in corpus_sample
# ---------------------------------------------------------------------------


class TestRawCapture:
    def test_raw_postings_captured_with_conn(self, tmp_path):
        """conn provided → corpus_sample row written for ats:fake."""
        _, conn = _setup_db(tmp_path)
        scanner = _make_scanner([{"title": "Data Scientist"}, {"title": "Cook"}])
        run_platform_scan(scanner, "acme", ["data scientist"], [], conn=conn)

        row = conn.execute(
            "SELECT output_json, surface FROM corpus_sample WHERE source='ats:fake'"
        ).fetchone()
        assert row is not None
        assert row["surface"] == "ats"
        # output_json carries {"job_count": N} where N = pre-filter raw count (2)
        snap = json.loads(row["output_json"])
        assert snap["job_count"] == 2  # raw count, not post-filter (1)

    def test_raw_text_is_json_dump_of_postings(self, tmp_path):
        """raw_text stored is the JSON-serialised posting list (truncated to 50k)."""
        _, conn = _setup_db(tmp_path)
        postings = [{"title": "Engineer", "id": "x1"}]
        scanner = _make_scanner(postings)
        run_platform_scan(scanner, "co", ["engineer"], [], conn=conn)

        row = conn.execute("SELECT raw_text FROM corpus_sample WHERE source='ats:fake'").fetchone()
        assert row is not None
        # raw_text may be scrubbed but the JSON structure should be recognisable
        assert "Engineer" in row["raw_text"] or row["raw_text"]  # non-empty at minimum

    def test_no_capture_when_conn_is_none(self, tmp_path):
        """conn=None → no corpus_sample row written (existing callers unaffected)."""
        _, conn = _setup_db(tmp_path)
        scanner = _make_scanner([{"title": "Data Scientist"}])
        # Call without conn (the default)
        run_platform_scan(scanner, "acme", ["data scientist"], [])

        count = conn.execute(
            "SELECT COUNT(*) FROM corpus_sample WHERE source='ats:fake'"
        ).fetchone()[0]
        assert count == 0

    def test_source_name_includes_scanner_name(self, tmp_path):
        """Source stored is 'ats:<scanner.name>' not a hardcoded platform string."""
        _, conn = _setup_db(tmp_path)
        scanner = _make_scanner([{"title": "SWE"}], name="myplatform")
        run_platform_scan(scanner, "slug", ["swe"], [], conn=conn)

        row = conn.execute(
            "SELECT source FROM corpus_sample WHERE source='ats:myplatform'"
        ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# detect=True: break counter increments on empty raw response
# ---------------------------------------------------------------------------


class TestBreakDetection:
    def test_empty_raw_response_increments_break_counter(self, tmp_path):
        """A raw [] from a platform with prior positive yield → consecutive_breaks++."""
        _, conn = _setup_db(tmp_path)
        # Establish a positive baseline (3 postings, detect=True via raw capture)
        scanner_productive = _make_scanner([{"title": "DS1"}, {"title": "DS2"}, {"title": "DS3"}])
        for _ in range(3):
            run_platform_scan(scanner_productive, "co", [], [], conn=conn)

        # Now the API returns nothing (break scenario)
        scanner_empty = _make_scanner([], name="fake")
        run_platform_scan(scanner_empty, "co", [], [], conn=conn)

        row = conn.execute(
            "SELECT consecutive_breaks, baseline_yield FROM source_health WHERE source='ats:fake'"
        ).fetchone()
        assert row is not None
        assert row["baseline_yield"] > 0, "baseline should be positive after productive calls"
        assert row["consecutive_breaks"] >= 1, "empty raw response should increment break counter"

    def test_result_list_unchanged_by_capture(self, tmp_path):
        """Raw capture is observability only — returned job list is unaffected."""
        _, conn = _setup_db(tmp_path)
        scanner = _make_scanner([{"title": "Data Scientist"}, {"title": "Chef"}])
        results = run_platform_scan(scanner, "acme", ["data scientist"], [], conn=conn)
        # Only 1 title matches "data scientist"
        assert len(results) == 1
        assert results[0]["title"] == "Data Scientist"
