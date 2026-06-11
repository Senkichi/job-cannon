"""Recency = COALESCE(posted_date, first_seen) in filters and sort (#365).

The posted_within / freshness filters and the 'recency' sort key rank jobs
by best-known posting date, falling back to detection time when no source
provided one. Staleness (last_seen) is a separate axis and is untouched.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from job_finder.db import get_filtered_jobs, upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
    os.remove(path)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _insert(conn, title, *, posted_date=None, first_seen=None):
    job = Job(
        title=title,
        company="Acme",
        # A resolvable City, ST — a bare "Remote" parses as an unresolved
        # JobLocation and the default unresolved-hide filter would mask the row.
        location="New York, NY",
        source="test",
        source_url=f"https://example.com/{title}",
        posted_date=posted_date,
        posted_date_precision="exact" if posted_date else None,
    )
    result = upsert_job(conn, ParsedJob.from_job(job))
    if first_seen is not None:
        # first_seen is system-owned; set directly for scenario control.
        conn.execute(
            "UPDATE jobs SET first_seen = ? WHERE dedup_key = ?",
            (first_seen.isoformat(), result.dedup_key),
        )
        conn.commit()
    return result.dedup_key


class TestPostedWithinUsesRecency:
    def test_old_detection_with_fresh_posted_date_matches(self, conn):
        """A job detected months ago but freshly (re)posted counts as recent."""
        _insert(conn, "Fresh Repost", posted_date=_now(), first_seen=_now() - timedelta(days=90))
        titles = [j["title"] for j in get_filtered_jobs(conn, posted_within="1w")]
        assert "Fresh Repost" in titles

    def test_undated_job_falls_back_to_first_seen(self, conn):
        """No posted_date → detection time keeps the job visible in filters."""
        _insert(conn, "Undated Recent")
        titles = [j["title"] for j in get_filtered_jobs(conn, posted_within="1w")]
        assert "Undated Recent" in titles

    def test_old_posted_date_excludes_despite_fresh_detection(self, conn):
        """A stale posting newly discovered is NOT 'recent' — posted_date wins."""
        _insert(conn, "Old Posting", posted_date=_now() - timedelta(days=60), first_seen=_now())
        titles = [j["title"] for j in get_filtered_jobs(conn, posted_within="1w")]
        assert "Old Posting" not in titles


class TestRecencySort:
    def test_recency_sort_orders_by_best_known_date(self, conn):
        _insert(conn, "Newest Posted", posted_date=_now() - timedelta(days=1))
        _insert(conn, "Mid Fallback", first_seen=_now() - timedelta(days=5))
        _insert(conn, "Oldest Posted", posted_date=_now() - timedelta(days=30))
        rows = get_filtered_jobs(conn, sort_by="recency", sort_dir="DESC")
        titles = [r["title"] for r in rows]
        assert titles.index("Newest Posted") < titles.index("Mid Fallback")
        assert titles.index("Mid Fallback") < titles.index("Oldest Posted")

    def test_bogus_sort_key_still_falls_back_to_score(self, conn):
        """Allowlist guard: unknown keys never reach the SQL string."""
        _insert(conn, "Any Job", posted_date=_now())
        rows = get_filtered_jobs(conn, sort_by="posted_date; DROP TABLE jobs--")
        assert [r["title"] for r in rows] == ["Any Job"]
        # jobs table survived the attempted injection
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
