"""Contract tests for upsert_job.

Verifies:
  1. ParsedJob insert -> kind="inserted", row lands in DB.
  2. ParsedJob with salary change on re-ingest -> kind="updated".
  3. ParsedJob identical re-ingest -> kind="unchanged".
  4. UnresolvedParsedJob -> row written; result.unresolved_reasons propagated.
  5. bool(UpsertResult) raises TypeError (D-19 requirement).
  6. Passing a raw Job raises TypeError (shim removed in Phase 48.07).

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md §11 commit 47.02, §12 commit 48.07
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.db._jobs import UpsertResult
from job_finder.models import Job
from job_finder.normalizers import normalize_company, normalize_title
from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


def _make_parsed_job(
    *,
    title: str = "Staff Engineer",
    company: str = "ContractTestCo",
    source: str = "linkedin",
    source_url: str = "https://linkedin.com/jobs/contract-test",
    description: str = "Short desc.",
    salary_min: int | None = None,
    salary_max: int | None = None,
) -> ParsedJob:
    """Direct ParsedJob construction — bypasses validators (unit-test only)."""
    dedup = _dedup_key(company, title)
    return ParsedJob(
        title=title,
        company=company,
        dedup_key=dedup,
        sources=[source],
        source_urls=[source_url],
        description=description,
        salary_min=salary_min,
        salary_max=salary_max,
    )


def _row_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — delete=False path reused across fixture; closed below, unlinked in finally
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


# ---------------------------------------------------------------------------
# 1. ParsedJob insert
# ---------------------------------------------------------------------------


def test_parsed_job_insert(conn: sqlite3.Connection) -> None:
    """Passing a ParsedJob for a brand-new dedup_key returns kind='inserted' and writes the row."""
    parsed = _make_parsed_job()
    result = upsert_job(conn, parsed)

    assert result.kind == "inserted"
    assert result.dedup_key == parsed.dedup_key
    assert _row_exists(conn, parsed.dedup_key)


# ---------------------------------------------------------------------------
# 2. ParsedJob update — salary change
# ---------------------------------------------------------------------------


def test_parsed_job_update_salary_change(conn: sqlite3.Connection) -> None:
    """Re-ingesting a ParsedJob with a new salary returns kind='updated'."""
    # First ingest: no salary
    parsed_no_salary = _make_parsed_job(title="Principal Engineer", salary_min=None)
    r1 = upsert_job(conn, parsed_no_salary)
    assert r1.kind == "inserted"

    # Second ingest: salary added → UPDATE branch detects change
    parsed_with_salary = _make_parsed_job(title="Principal Engineer", salary_min=180_000)
    r2 = upsert_job(conn, parsed_with_salary)
    assert r2.kind == "updated"
    assert r2.dedup_key == parsed_no_salary.dedup_key


# ---------------------------------------------------------------------------
# 3. ParsedJob unchanged — identical re-ingest
# ---------------------------------------------------------------------------


def test_parsed_job_unchanged_on_identical_reingest(conn: sqlite3.Connection) -> None:
    """Re-ingesting a ParsedJob with identical fields returns kind='unchanged'."""
    parsed = _make_parsed_job(title="Senior Scientist")
    r1 = upsert_job(conn, parsed)
    assert r1.kind == "inserted"

    # Same object, same dedup_key, same source, same description, no salary
    r2 = upsert_job(conn, parsed)
    assert r2.kind == "unchanged"
    assert r2.dedup_key == parsed.dedup_key


# ---------------------------------------------------------------------------
# 4. Raw Job is rejected — shim removed in Phase 48.07
# ---------------------------------------------------------------------------


def test_raw_job_raises_type_error(conn: sqlite3.Connection) -> None:
    """Passing a raw Job to upsert_job raises TypeError (shim removed in Phase 48.07).

    Callers must construct ParsedJob.from_job(job) before calling upsert_job.
    """
    job = Job(
        title="Data Scientist",
        company="ShimTestCo",
        location="Remote",
        source="serpapi",
        source_url="https://serpapi.com/jobs/shim-test",
    )
    with pytest.raises(TypeError, match="ParsedJob"):
        upsert_job(conn, job)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. UnresolvedParsedJob — row written; unresolved_reasons propagated
# ---------------------------------------------------------------------------


def test_unresolved_parsed_job_written_with_reasons(conn: sqlite3.Connection) -> None:
    """UnresolvedParsedJob is written to DB; result.unresolved_reasons carries reason codes."""
    dedup = _dedup_key("UnresolvedCo", "Software Engineer) CA")
    unresolved = UnresolvedParsedJob(
        title="Software Engineer) CA",
        company="UnresolvedCo",
        dedup_key=dedup,
        sources=["linkedin"],
        source_urls=["https://linkedin.com/jobs/unresolved"],
        description="Short desc.",
        raw_title="Software Engineer) CA",
        unresolved_reasons=["title_metadata_blob"],
    )
    result = upsert_job(conn, unresolved)

    # Row is written despite unresolved status
    assert _row_exists(conn, dedup)
    # kind reflects insert (new row)
    assert result.kind == "inserted"
    # Reason codes are carried in the result (not yet persisted to DB — Phase 47.04)
    assert result.unresolved_reasons == ["title_metadata_blob"]


# ---------------------------------------------------------------------------
# 6. UpsertResult.__bool__ raises TypeError (D-19)
# ---------------------------------------------------------------------------


def test_upsert_result_bool_raises_type_error(conn: sqlite3.Connection) -> None:
    """bool(UpsertResult) raises TypeError — callers must use result.kind."""
    parsed = _make_parsed_job(title="Bool Test Engineer")
    result = upsert_job(conn, parsed)

    with pytest.raises(TypeError, match="not bool-testable"):
        bool(result)


def test_upsert_result_has_explicit_bool_guard() -> None:
    """UpsertResult defines __bool__ to raise TypeError (not merely absent)."""
    # Construct a result directly to verify the guard without a DB
    result = UpsertResult(kind="inserted", dedup_key="test|key")
    with pytest.raises(TypeError):
        bool(result)
