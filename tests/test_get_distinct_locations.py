"""Tests for job_finder.db.get_distinct_locations.

The filter dropdown reads from this helper. It must:
- Source from per-entry locations_raw, NOT the merged location column,
  so multi-location combinations don't bloat the dropdown.
- Apply normalize_location to each entry.
- Lower-case-dedupe so case variants collapse to one entry.
- Return results sorted case-insensitively.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from job_finder.db import get_distinct_locations
from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def migrated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert(conn: sqlite3.Connection, dedup_key: str, locs: list[str]) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, locations_raw,
               first_seen, last_seen)
            VALUES (?, 'Engineer', 'X', ?, '[]',
                    NULL, 'discovered', '["test"]', ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, ", ".join(locs), json.dumps(locs)),
    )
    conn.commit()


class TestDistinctLocations:
    def test_returns_individual_entries_not_merged_combinations(self, migrated_db):
        """Two jobs with overlapping location sets should produce a clean
        set of individual entries, not a separate entry per multi-location
        combination."""
        _, conn = migrated_db
        _insert(conn, "j1", ["Remote", "NYC"])
        _insert(conn, "j2", ["Remote", "SF"])
        _insert(conn, "j3", ["NYC", "SF", "Remote"])
        result = get_distinct_locations(conn)
        assert sorted(result, key=str.lower) == ["NYC", "Remote", "SF"]

    def test_case_insensitively_dedupes(self, migrated_db):
        _, conn = migrated_db
        _insert(conn, "j1", ["Remote"])
        _insert(conn, "j2", ["remote"])
        _insert(conn, "j3", ["REMOTE"])
        result = get_distinct_locations(conn)
        assert len(result) == 1
        # Display uses the first-seen casing
        assert result[0].lower() == "remote"

    def test_skips_placeholder_entries(self, migrated_db):
        _, conn = migrated_db
        _insert(conn, "j1", ["Unknown"])
        _insert(conn, "j2", ["TBD", "San Francisco, CA"])
        _insert(conn, "j3", ["N/A"])
        result = get_distinct_locations(conn)
        assert result == ["San Francisco, CA"]

    def test_handles_empty_locations_raw_gracefully(self, migrated_db):
        _, conn = migrated_db
        _insert(conn, "j1", [])
        _insert(conn, "j2", ["Remote"])
        result = get_distinct_locations(conn)
        assert result == ["Remote"]

    def test_skips_invalid_json(self, migrated_db):
        _, conn = migrated_db
        # Directly insert a row with malformed JSON in locations_raw
        conn.execute(
            """INSERT INTO jobs
                  (dedup_key, title, company, location, source_urls,
                   jd_full, pipeline_status, sources, locations_raw,
                   first_seen, last_seen)
                VALUES ('j_bad', 'Engineer', 'X', 'Remote', '[]',
                        NULL, 'discovered', '["test"]', 'not-json',
                        '2026-01-01', '2026-01-01')"""
        )
        _insert(conn, "j_good", ["Remote"])
        conn.commit()
        result = get_distinct_locations(conn)
        assert result == ["Remote"]  # malformed row silently skipped

    def test_returns_sorted_case_insensitively(self, migrated_db):
        _, conn = migrated_db
        _insert(conn, "j1", ["zurich"])
        _insert(conn, "j2", ["Atlanta"])
        _insert(conn, "j3", ["boston"])
        result = get_distinct_locations(conn)
        # Lower-case alpha sort: atlanta, boston, zurich
        assert [v.lower() for v in result] == ["atlanta", "boston", "zurich"]

    def test_empty_db_returns_empty_list(self, migrated_db):
        _, conn = migrated_db
        assert get_distinct_locations(conn) == []
