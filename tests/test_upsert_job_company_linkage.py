"""upsert_job inline company_id attachment + denylist boundary.

The ATS scanner (`_run.py`, `_run_html.py`) and careers crawler
(`_persistence.py`) already know the `company_id` when calling
`upsert_job` — they're iterating companies rows directly. Passing
`company_id=` attaches the FK at write time instead of waiting up to
24 h for the daily company-linkage backfill.

Separately, `upsert_job` short-circuits when the normalized company
name is in COMPANY_DENYLIST (aggregator placeholders like "Jobgether"
/ "Mercor" / "RemoteHunter" that appear in source feeds but have no
real ATS presence). Returning False without writing prevents these
rows from polluting the jobs table.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import DenylistedCompanyError, ParsedJob
from job_finder.web.db_migrate import run_migrations


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — explicit close+unlink to share path with sqlite3.connect
    tmp.close()
    path = Path(tmp.name)
    try:
        run_migrations(str(path))
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        yield c
        c.close()
    finally:
        path.unlink(missing_ok=True)


def _make_job(*, title: str = "Senior Eng", company: str = "TestCo") -> Job:
    return Job(
        title=title,
        company=company,
        location="San Francisco, CA",
        source="lever",
        source_url=f"https://example.com/j/{title}",
        description="x" * 250,
    )


def _to_parsed(job: Job) -> ParsedJob:
    """Convert a Job to ParsedJob for upsert_job (Phase 48.07 contract)."""
    return ParsedJob.from_job(job)  # type: ignore[return-value]


def _read_company_id(conn: sqlite3.Connection, dedup_key: str) -> int | None:
    r = conn.execute("SELECT company_id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return r["company_id"] if r else None


def _job_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone() is not None
    )


# ---------- company_id attachment ----------


class TestInsertCompanyId:
    def test_insert_with_company_id_attaches_fk(self, conn: sqlite3.Connection):
        upsert_job(conn, _to_parsed(_make_job(title="a")), company_id=42)
        assert _read_company_id(conn, "testco|a") == 42

    def test_insert_without_company_id_leaves_null(self, conn: sqlite3.Connection):
        upsert_job(conn, _to_parsed(_make_job(title="b")))
        assert _read_company_id(conn, "testco|b") is None


class TestUpdateCompanyId:
    def test_update_attaches_company_id_when_existing_null(self, conn: sqlite3.Connection):
        # First insert: no company_id.
        upsert_job(conn, _to_parsed(_make_job(title="c")))
        assert _read_company_id(conn, "testco|c") is None
        # Re-ingest with company_id (e.g. ATS scanner now knows it).
        upsert_job(conn, _to_parsed(_make_job(title="c")), company_id=99)
        assert _read_company_id(conn, "testco|c") == 99

    def test_update_preserves_existing_company_id_when_new_is_none(self, conn: sqlite3.Connection):
        # First insert sets the FK.
        upsert_job(conn, _to_parsed(_make_job(title="d")), company_id=42)
        # Re-ingest without company_id (e.g. email parser path).
        upsert_job(conn, _to_parsed(_make_job(title="d")))
        # COALESCE preserves the existing FK.
        assert _read_company_id(conn, "testco|d") == 42


# ---------- denylist boundary ----------


class TestDenylistRejects:
    @pytest.mark.parametrize(
        "name",
        [
            "Jobgether",
            "jobgether",  # case
            "Mercor",
            "RemoteHunter",
            "Crossing Hurdles",
            "Unknown",
            "Medical Jobs",
        ],
    )
    def test_denylisted_company_drops_at_boundary(self, conn: sqlite3.Connection, name: str):
        """Phase 48.07: denylist rejection now happens in ParsedJob.from_job, not upsert_job."""
        with pytest.raises(DenylistedCompanyError):
            ParsedJob.from_job(_make_job(title="x", company=name))
        # Verify no row landed.
        assert not _job_exists(conn, Job.normalized_dedup_key(name, "x"))

    def test_real_company_persists(self, conn: sqlite3.Connection):
        upsert_job(conn, _to_parsed(_make_job(title="y", company="Stripe")))
        assert _job_exists(conn, Job.normalized_dedup_key("Stripe", "y"))
