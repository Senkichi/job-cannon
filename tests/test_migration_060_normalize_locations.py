"""Tests for Migration 60 — heal location pollution.

Covers:
- Whitespace / case / punctuation variants collapse to one entry.
- Placeholder entries ("Unknown", "TBD") are dropped.
- Multi-location entries stored as one string ("Remote | NYC") are split.
- The merged ``location`` column is rebuilt from the cleaned list.
- Idempotent re-run is a no-op.
- No-op on a fresh empty database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m060_normalize_locations import (
    MIGRATION,
    _heal_locations,
)
from job_finder.web.migrations.types import MigrationContext


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


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    location: str,
    locations_raw: list[str],
) -> str:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, locations_raw,
               first_seen, last_seen)
            VALUES (?, 'Engineer', 'X', ?, '[]',
                    NULL, 'discovered', '["test"]', ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, location, json.dumps(locations_raw)),
    )
    conn.commit()
    return dedup_key


def _read_job(conn: sqlite3.Connection, dedup_key: str) -> tuple[str, list[str]]:
    row = conn.execute(
        "SELECT location, locations_raw FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return row["location"], json.loads(row["locations_raw"])


def _run_m060(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=59,
    )
    _heal_locations(ctx)
    conn.commit()


def test_migration_declares_version_60():
    assert MIGRATION.version == 60


class TestNormalizationApplied:
    def test_collapses_case_and_whitespace_duplicates(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|dupes",
            location="Remote, remote, REMOTE",
            locations_raw=["Remote", "remote", "REMOTE", "  Remote  "],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|dupes")
        assert locs_raw == ["Remote"]
        assert loc == "Remote"

    def test_drops_placeholder_entries(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|placeholders",
            location="Unknown, TBD, San Francisco, CA",
            locations_raw=["Unknown", "TBD", "San Francisco, CA"],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|placeholders")
        assert locs_raw == ["San Francisco, CA"]
        assert loc == "San Francisco, CA"

    def test_all_placeholders_clears_to_empty_list(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|all_junk",
            location="Unknown, TBD, N/A",
            locations_raw=["Unknown", "TBD", "N/A"],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|all_junk")
        assert locs_raw == []
        assert loc == ""

    def test_splits_multi_location_pipe_separated_entry(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|multi",
            location="Remote | NYC | San Francisco",
            locations_raw=["Remote | NYC | San Francisco"],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|multi")
        assert locs_raw == ["Remote", "NYC", "San Francisco"]
        assert loc == "Remote, NYC, San Francisco"

    def test_preserves_city_state_pairs_with_commas(self, migrated_db):
        """City/State pairs use ',' — must NOT be split into garbage."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|citystate",
            location="San Francisco, CA",
            locations_raw=["San Francisco, CA"],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|citystate")
        assert locs_raw == ["San Francisco, CA"]
        assert loc == "San Francisco, CA"

    def test_does_not_touch_already_clean_rows(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|clean",
            location="Remote",
            locations_raw=["Remote"],
        )
        _run_m060(path, conn)
        loc, locs_raw = _read_job(conn, "test|clean")
        assert locs_raw == ["Remote"]
        assert loc == "Remote"


class TestIdempotence:
    def test_second_run_changes_nothing(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "test|round_trip",
            location="REMOTE, Unknown, San Francisco, CA",
            locations_raw=["REMOTE", "Unknown", "San Francisco, CA"],
        )
        _run_m060(path, conn)
        after_first = _read_job(conn, "test|round_trip")

        _run_m060(path, conn)
        after_second = _read_job(conn, "test|round_trip")

        assert after_first == after_second


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m060(path, conn)  # should not raise
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0
