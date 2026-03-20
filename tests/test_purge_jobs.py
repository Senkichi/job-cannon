"""Tests for purge_jobs.py — standalone CLI script to purge bulk-load spike jobs.

Tests cover all PURGE requirements:
- PURGE-01: dry-run prints spike info without deleting
- PURGE-02: JSON export before DELETE
- PURGE-03: child-table cleanup (scoring_costs, pipeline_events, pipeline_detections)
- PURGE-04: post-purge job count is positive and less than pre-purge count
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from job_finder.web.db_migrate import run_migrations

# The bulk-load sentinel timestamp
BULK_TS = "2026-03-11T00:07:56.816925"

# Organic timestamps (should survive purge)
ORGANIC_TS_1 = "2026-03-08T14:22:00.000000"
ORGANIC_TS_2 = "2026-03-09T10:00:00.000000"
ORGANIC_TS_3 = "2026-03-10T08:30:00.000000"


def make_migrated_db():
    """Create a fresh migrated temp DB, return (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def seed_spike_jobs(conn: sqlite3.Connection) -> list[str]:
    """Insert 10 spike jobs + 3 organic jobs. Return list of spike dedup_keys.

    Spike jobs: 8 with pipeline_status='discovered', 1 with 'applied', 1 with 'reviewing'
    Organic jobs: 3 with different first_seen timestamps (should survive purge)
    """
    spike_keys = []
    spike_rows = []
    for i in range(10):
        key = f"spike-company-{i}|spike job title {i}|remote"
        status = "applied" if i == 0 else ("reviewing" if i == 1 else "discovered")
        spike_keys.append(key)
        spike_rows.append((
            key,
            f"Spike Job Title {i}",
            f"Spike Company {i}",
            "Remote",
            '["linkedin"]',
            f'["https://linkedin.com/jobs/{i}"]',
            str(i),
            100000, 150000,
            f"Description for spike job {i}",
            BULK_TS,
            BULK_TS,
            0.0, "{}",
            "interested",
            status,
        ))

    organic_rows = [
        (
            "organic-acme|data scientist|remote",
            "Data Scientist",
            "Organic Acme",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/1001"]',
            "1001",
            120000, 180000,
            "Organic job description 1",
            ORGANIC_TS_1,
            ORGANIC_TS_1,
            0.0, "{}",
            "unreviewed",
            "discovered",
        ),
        (
            "organic-beta|senior data scientist|new york",
            "Senior Data Scientist",
            "Organic Beta",
            "New York",
            '["glassdoor"]',
            '["https://glassdoor.com/jobs/1002"]',
            "1002",
            150000, 200000,
            "Organic job description 2",
            ORGANIC_TS_2,
            ORGANIC_TS_2,
            0.0, "{}",
            "unreviewed",
            "discovered",
        ),
        (
            "organic-gamma|staff data scientist|san francisco",
            "Staff Data Scientist",
            "Organic Gamma",
            "San Francisco",
            '["ziprecruiter"]',
            '["https://ziprecruiter.com/jobs/1003"]',
            "1003",
            180000, 250000,
            "Organic job description 3",
            ORGANIC_TS_3,
            ORGANIC_TS_3,
            0.0, "{}",
            "unreviewed",
            "discovered",
        ),
    ]

    all_rows = spike_rows + organic_rows
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, user_interest,
             pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        all_rows,
    )
    conn.commit()
    return spike_keys


def seed_child_rows(conn: sqlite3.Connection, spike_keys: list[str]) -> None:
    """Insert scoring_costs, pipeline_events, and pipeline_detections rows for spike jobs."""
    now = "2026-03-11T01:00:00"

    # scoring_costs: insert for first 3 spike keys
    for key in spike_keys[:3]:
        conn.execute(
            """INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, "haiku_score", "claude-haiku-4-5", 500, 100, 0.001, now),
        )

    # pipeline_events: insert for first 3 spike keys
    for key in spike_keys[:3]:
        conn.execute(
            """INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source)
               VALUES (?, ?, ?, ?, ?)""",
            (key, "discovered", "reviewing", now, "manual"),
        )

    # pipeline_detections: insert for first 2 spike keys
    for i, key in enumerate(spike_keys[:2]):
        conn.execute(
            """INSERT INTO pipeline_detections
                (gmail_message_id, detection_type, job_id, confidence_score,
                 matched_signals, snippet, email_subject, email_from,
                 email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"msg-spike-{i}",
                "interview_request",
                key,
                90,
                "[]",
                "Interview snippet",
                "Interview Request",
                "recruiter@company.com",
                now,
                "pending",
                now,
            ),
        )

    conn.commit()


# ─────────────────────── Tests ───────────────────────


def test_identify_spike():
    """identify_spike returns correct count, sample_titles, and non_discovered."""
    from purge_jobs import identify_spike

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        result = identify_spike(conn)

        assert result["count"] == 10, f"Expected 10 spike jobs, got {result['count']}"
        assert len(result["sample_titles"]) >= 5, "Expected at least 5 sample titles"
        assert len(result["non_discovered"]) == 2, (
            f"Expected 2 non-discovered jobs (applied + reviewing), got {len(result['non_discovered'])}"
        )
        # Verify dedup_keys are all from spike
        assert set(result["dedup_keys"]) == set(spike_keys)
        # Verify date_range is a tuple/list
        assert result["date_range"] is not None
    finally:
        conn.close()
        os.remove(path)


def test_identify_spike_empty_db():
    """identify_spike returns count=0 when no spike jobs exist."""
    from purge_jobs import identify_spike

    path, conn = make_migrated_db()
    try:
        # Only organic jobs
        seed_spike_jobs(conn)
        # Remove all spike jobs first
        conn.execute("DELETE FROM jobs WHERE first_seen = ?", (BULK_TS,))
        conn.commit()
        result = identify_spike(conn)
        assert result["count"] == 0
        assert result["dedup_keys"] == []
    finally:
        conn.close()
        os.remove(path)


def test_dry_run_no_deletion():
    """Dry-run mode (calling identify_spike only) does not delete any rows."""
    from purge_jobs import identify_spike

    path, conn = make_migrated_db()
    try:
        seed_spike_jobs(conn)
        pre_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        # Simulate dry-run: call identify_spike but do NOT call purge
        result = identify_spike(conn)
        assert result["count"] == 10

        post_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert pre_count == post_count, (
            f"Dry-run should not delete jobs. Before: {pre_count}, After: {post_count}"
        )
    finally:
        conn.close()
        os.remove(path)


def test_export_creates_json(tmp_path):
    """export_to_json creates a file with correct row count and required columns."""
    from purge_jobs import export_to_json

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)

        # Use tmp_path as the output dir so we don't pollute data/
        json_path = export_to_json(conn, spike_keys, output_dir=str(tmp_path))

        assert os.path.exists(json_path), f"JSON file not found: {json_path}"

        with open(json_path, "r") as f:
            rows = json.load(f)

        assert len(rows) == 10, f"Expected 10 rows in JSON, got {len(rows)}"

        # Verify required columns present in every row
        for row in rows:
            assert "dedup_key" in row, "Missing dedup_key column"
            assert "title" in row, "Missing title column"
            assert "company" in row, "Missing company column"
    finally:
        conn.close()
        os.remove(path)


def test_export_precedes_delete(tmp_path):
    """export_to_json creates file before any DELETE is called."""
    from purge_jobs import export_to_json, purge

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)

        # Export THEN purge — file must exist after purge completes
        json_path = export_to_json(conn, spike_keys, output_dir=str(tmp_path))
        purge(conn, spike_keys)

        assert os.path.exists(json_path), "JSON file must exist even after purge"

        with open(json_path, "r") as f:
            rows = json.load(f)
        assert len(rows) == 10
    finally:
        conn.close()
        os.remove(path)


def test_orphan_cleanup_scoring_costs():
    """purge() deletes all scoring_costs rows for purged spike keys."""
    from purge_jobs import purge

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        seed_child_rows(conn, spike_keys)

        # Verify child rows exist before purge
        pre_cost_rows = conn.execute(
            "SELECT COUNT(*) FROM scoring_costs WHERE job_id IN ({})".format(
                ",".join("?" * len(spike_keys))
            ),
            spike_keys,
        ).fetchone()[0]
        assert pre_cost_rows == 3, f"Expected 3 scoring_costs rows, got {pre_cost_rows}"

        purge(conn, spike_keys)

        post_cost_rows = conn.execute(
            "SELECT COUNT(*) FROM scoring_costs WHERE job_id IN ({})".format(
                ",".join("?" * len(spike_keys))
            ),
            spike_keys,
        ).fetchone()[0]
        assert post_cost_rows == 0, (
            f"Expected 0 scoring_costs rows after purge, got {post_cost_rows}"
        )
    finally:
        conn.close()
        os.remove(path)


def test_orphan_cleanup_pipeline_events():
    """purge() deletes all pipeline_events rows for purged spike keys."""
    from purge_jobs import purge

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        seed_child_rows(conn, spike_keys)

        purge(conn, spike_keys)

        post_event_rows = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id IN ({})".format(
                ",".join("?" * len(spike_keys))
            ),
            spike_keys,
        ).fetchone()[0]
        assert post_event_rows == 0, (
            f"Expected 0 pipeline_events rows after purge, got {post_event_rows}"
        )
    finally:
        conn.close()
        os.remove(path)


def test_orphan_cleanup_pipeline_detections():
    """purge() deletes all pipeline_detections rows for purged spike keys."""
    from purge_jobs import purge

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        seed_child_rows(conn, spike_keys)

        purge(conn, spike_keys)

        post_detection_rows = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE job_id IN ({})".format(
                ",".join("?" * len(spike_keys))
            ),
            spike_keys,
        ).fetchone()[0]
        assert post_detection_rows == 0, (
            f"Expected 0 pipeline_detections rows after purge, got {post_detection_rows}"
        )
    finally:
        conn.close()
        os.remove(path)


def test_post_purge_count():
    """After purge(), organic jobs survive (0 < post_count < pre_count)."""
    from purge_jobs import purge

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        pre_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert pre_count == 13, f"Expected 13 total jobs (10 spike + 3 organic), got {pre_count}"

        purge(conn, spike_keys)

        post_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert post_count > 0, "Post-purge count must be > 0 (organic jobs should survive)"
        assert post_count < pre_count, (
            f"Post-purge count {post_count} must be less than pre-purge {pre_count}"
        )
        assert post_count == 3, f"Expected exactly 3 organic jobs to survive, got {post_count}"
    finally:
        conn.close()
        os.remove(path)


def test_verify_catches_orphans():
    """verify() raises AssertionError when orphaned child rows remain."""
    from purge_jobs import verify

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        seed_child_rows(conn, spike_keys)

        pre_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        # Delete spike jobs but leave scoring_costs (orphaned)
        placeholders = ",".join("?" * len(spike_keys))
        conn.execute(f"DELETE FROM jobs WHERE dedup_key IN ({placeholders})", spike_keys)
        conn.commit()

        # verify() should catch the orphaned scoring_costs rows
        with pytest.raises(AssertionError, match="orphan"):
            verify(conn, pre_count, spike_keys)
    finally:
        conn.close()
        os.remove(path)


def test_verify_passes_clean_state():
    """verify() passes when no orphans remain and count is in range."""
    from purge_jobs import purge, verify

    path, conn = make_migrated_db()
    try:
        spike_keys = seed_spike_jobs(conn)
        seed_child_rows(conn, spike_keys)

        pre_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        purge(conn, spike_keys)

        # Should not raise
        verify(conn, pre_count, spike_keys)
    finally:
        conn.close()
        os.remove(path)
