"""Tests for crawl latency SLI dashboard query."""

import sqlite3

import pytest

from job_finder.db import get_crawl_latency_sli


def test_p50_p95_p99_from_known_ladder(migrated_db):
    """Percentiles computed correctly from known latency ladder."""
    path, conn = migrated_db

    # Insert exact rows with known latencies: [1, 1, 2, 5, 10] days
    # julianday works with date strings; use whole days for simplicity
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, posted_date, posted_date_precision, sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("job1", "Job 1", "Co1", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
            ("job2", "Job 2", "Co2", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
            ("job3", "Job 3", "Co3", "Remote", "2026-01-03", "2026-01-03", "2026-01-01", "exact", '["ats"]'),
            ("job4", "Job 4", "Co4", "Remote", "2026-01-06", "2026-01-06", "2026-01-01", "exact", '["ats"]'),
            ("job5", "Job 5", "Co5", "Remote", "2026-01-11", "2026-01-11", "2026-01-01", "exact", '["ats"]'),
        ],
    )
    conn.commit()

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Latencies: [1, 1, 2, 5, 10] → p50=2, p95=10, p99=10
    assert result["sample_n"] == 5
    assert result["p50_days"] == 2.0
    assert result["p95_days"] == 10.0
    assert result["p99_days"] == 10.0
    # No mean/avg key (percentiles only)
    assert "mean" not in result
    assert "avg" not in result


def test_copy_row_excluded(migrated_db):
    """Rows where posted_date == first_seen are excluded (m095-copy guard)."""
    path, conn = migrated_db

    # One copy row (posted_date == first_seen) and one real row
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, posted_date, posted_date_precision, sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("copy", "Copy", "Co1", "Remote", "2026-01-01", "2026-01-01", "2026-01-01", "exact", '["ats"]'),
            ("real", "Real", "Co2", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
        ],
    )
    conn.commit()

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only the real row qualifies
    assert result["sample_n"] == 1
    assert result["p50_days"] == 1.0
    # Copy row does NOT create a 0-day floor
    assert result["p50_days"] != 0.0


def test_proxy_and_approximate_excluded_from_headline(migrated_db):
    """proxy/approximate precision rows excluded from headline, counted in total_dated."""
    path, conn = migrated_db

    # Mix of exact, proxy, approximate
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, posted_date, posted_date_precision, sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("exact1", "Exact 1", "Co1", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
            ("exact2", "Exact 2", "Co2", "Remote", "2026-01-03", "2026-01-03", "2026-01-01", "exact", '["ats"]'),
            ("proxy", "Proxy", "Co3", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "proxy", '["linkedin"]'),
            ("approx", "Approx", "Co4", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "approximate", '["indeed"]'),
        ],
    )
    conn.commit()

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only exact rows qualify for headline
    assert result["sample_n"] == 2
    assert result["total_dated"] == 4
    # Coverage reflects the drop
    assert result["exact_coverage_pct"] == 50.0


def test_cold_start_backlog_excluded(migrated_db):
    """Rows with latency > cold_start_exclude_days are excluded."""
    path, conn = migrated_db

    # One normal row (1 day), one backlog row (90 days)
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, posted_date, posted_date_precision, sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("normal", "Normal", "Co1", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
            ("backlog", "Backlog", "Co2", "Remote", "2026-04-01", "2026-04-01", "2026-01-01", "exact", '["ats"]'),
        ],
    )
    conn.commit()

    # Default 30-day window excludes backlog
    config = {}
    result = get_crawl_latency_sli(conn, config)
    assert result["sample_n"] == 1
    assert result["cold_start_exclude_days"] == 30

    # 120-day window includes backlog
    config = {"metrics": {"crawl_latency": {"cold_start_exclude_days": 120}}}
    result = get_crawl_latency_sli(conn, config)
    assert result["sample_n"] == 2
    assert result["cold_start_exclude_days"] == 120


def test_negative_latency_excluded(migrated_db):
    """Negative latencies (clock skew: posted_date > first_seen) are excluded."""
    path, conn = migrated_db

    # One normal row, one negative latency row
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, posted_date, posted_date_precision, sources)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("normal", "Normal", "Co1", "Remote", "2026-01-02", "2026-01-02", "2026-01-01", "exact", '["ats"]'),
            ("skew", "Skew", "Co2", "Remote", "2026-01-01", "2026-01-01", "2026-01-02", "exact", '["ats"]'),
        ],
    )
    conn.commit()

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only normal row qualifies
    assert result["sample_n"] == 1
    assert result["p50_days"] == 1.0


def test_empty_set_returns_none_percentiles(migrated_db):
    """No qualifying rows → None percentiles, sample_n=0, no crash."""
    path, conn = migrated_db

    config = {}
    result = get_crawl_latency_sli(conn, config)

    assert result["sample_n"] == 0
    assert result["total_dated"] == 0
    assert result["p50_days"] is None
    assert result["p95_days"] is None
    assert result["p99_days"] is None
    assert result["exact_coverage_pct"] == 0.0


def test_pre_m095_db_graceful(tmp_db_path):
    """Pre-m095 DB (missing posted_date_precision column) returns zeroed dict."""
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row

    # Create pre-m095 schema (no posted_date_precision)
    conn.execute(
        """CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            posted_date TEXT
        )"""
    )
    conn.commit()

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Zeroed dict, no exception
    assert result["sample_n"] == 0
    assert result["total_dated"] == 0
    assert result["p50_days"] is None
    assert result["p95_days"] is None
    assert result["p99_days"] is None
    assert result["exact_coverage_pct"] == 0.0

    conn.close()
