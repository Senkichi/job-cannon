"""Contract tests for upsert_job (Phase 47.02 + Phase 48.07).

Verifies:
  1. ParsedJob insert → kind="inserted", row lands in DB.
  2. ParsedJob with salary change on re-ingest → kind="updated".
  3. ParsedJob identical re-ingest → kind="unchanged".
  4. ParsedJob built via from_job(Job(...)) → equivalent insert + touch
     behavior (the call shape every caller now uses post-48.07).
  5. UnresolvedParsedJob → row written; result.unresolved_reasons propagated.
  6. bool(UpsertResult) raises TypeError (D-19 requirement).

Phase 48.07 removed the Job→ParsedJob shim from upsert_job itself; the
former "Job shim" cases now go through ParsedJob.from_job() at the
caller boundary. The behavioral expectations are unchanged.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md
§11 commit 47.02; §12 commit 48.07.
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
# 4. Caller-boundary conversion: ParsedJob.from_job(Job(...))
# ---------------------------------------------------------------------------


def test_from_job_insert(conn: sqlite3.Connection) -> None:
    """The post-48.07 caller shape: Job → ParsedJob.from_job → upsert_job."""
    job = Job(
        title="Data Scientist",
        company="ShimTestCo",
        location="Remote",
        source="serpapi",
        source_url="https://serpapi.com/jobs/shim-test",
    )
    parsed = ParsedJob.from_job(job)
    result = upsert_job(conn, parsed)

    assert result.kind == "inserted"
    assert result.dedup_key == job.dedup_key
    assert _row_exists(conn, job.dedup_key)


def test_from_job_touch(conn: sqlite3.Connection) -> None:
    """Re-ingesting the same dedup_key with only a new source returns kind='touched'.

    Per D-15 (Phase 47.09), a re-sighting that adds a source but changes no
    canonical field is the touch path, not an update.
    """
    job = Job(
        title="ML Engineer",
        company="ShimUpdateCo",
        location="Remote",
        source="linkedin",
        source_url="https://linkedin.com/jobs/shim-update",
    )
    r1 = upsert_job(conn, ParsedJob.from_job(job))
    assert r1.kind == "inserted"

    job2 = Job(
        title="ML Engineer",
        company="ShimUpdateCo",
        location="Remote",
        source="dataforseo",  # new source only, no canonical change → "touched"
        source_url="https://dataforseo.com/jobs/shim-update",
    )
    r2 = upsert_job(conn, ParsedJob.from_job(job2))
    assert r2.kind == "touched"


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


# ---------------------------------------------------------------------------
# 7. Issue #219 — (company_id, source_id) merge path (I-11 collision)
# ---------------------------------------------------------------------------


def test_upsert_merges_on_company_source_id_when_dedup_key_misses(
    conn: sqlite3.Connection,
) -> None:
    """A second posting with a new title but the same (company_id, source_id)
    merges into the first row instead of tripping the I-11 partial UNIQUE index.

    Repro of the N12 finding: Workday tenants surface a stable externalPath
    (source_id) under drifting display titles. Two distinct dedup_keys collide
    on (company_id, source_id); without the merge path the second INSERT is
    rejected as I-11 and the posting is silently dropped.
    """
    # First posting: title A, source_id "abc"
    parsed_a = ParsedJob(
        title="Brand Campaign Operations Sr. Analyst",
        company="WorkdayCo",
        dedup_key=_dedup_key("WorkdayCo", "Brand Campaign Operations Sr. Analyst"),
        sources=["greenhouse"],
        source_urls=["https://example.com/jobs/abc?title=analyst"],
        source_id="/job/Mexico---Mexico-City/Brand-Campaign-Operations_R-12345",
    )
    r1 = upsert_job(conn, parsed_a, company_id=42)
    assert r1.kind == "inserted"

    # Second posting: title drifted, same (company_id, source_id) — distinct
    # dedup_key, would historically take the INSERT branch → I-11 violation.
    parsed_b = ParsedJob(
        title="Brand Campaign Operations Manager",
        company="WorkdayCo",
        dedup_key=_dedup_key("WorkdayCo", "Brand Campaign Operations Manager"),
        sources=["greenhouse"],
        source_urls=["https://example.com/jobs/abc?title=manager"],
        source_id="/job/Mexico---Mexico-City/Brand-Campaign-Operations_R-12345",
    )
    r2 = upsert_job(conn, parsed_b, company_id=42)

    # Acceptance: no IngestionRejected, merge kind, matched row's dedup_key
    # returned (not the incoming one), and B's source_url present on the row.
    assert r2.kind in {"updated", "touched", "unchanged"}
    assert r2.dedup_key == parsed_a.dedup_key

    rows = conn.execute(
        "SELECT dedup_key, source_urls FROM jobs WHERE source_id = ?",
        (parsed_a.source_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == parsed_a.dedup_key
    assert "https://example.com/jobs/abc?title=manager" in rows[0]["source_urls"]


def test_upsert_still_inserts_when_source_id_is_empty(conn: sqlite3.Connection) -> None:
    """The partial UNIQUE exemption is preserved: rows with NULL/'' source_id
    still insert independently within the same company. The merge fallback
    only fires when parsed.source_id is truthy.
    """
    parsed_a = ParsedJob(
        title="Engineer One",
        company="NoSidCo",
        dedup_key=_dedup_key("NoSidCo", "Engineer One"),
        sources=["serpapi"],
        source_urls=["https://example.com/1"],
        source_id=None,
    )
    parsed_b = ParsedJob(
        title="Engineer Two",
        company="NoSidCo",
        dedup_key=_dedup_key("NoSidCo", "Engineer Two"),
        sources=["serpapi"],
        source_urls=["https://example.com/2"],
        source_id=None,
    )
    r1 = upsert_job(conn, parsed_a, company_id=99)
    r2 = upsert_job(conn, parsed_b, company_id=99)
    assert r1.kind == "inserted"
    assert r2.kind == "inserted"

    count = conn.execute("SELECT COUNT(*) FROM jobs WHERE company_id = 99").fetchone()[0]
    assert count == 2


def test_upsert_does_not_merge_when_company_id_missing(conn: sqlite3.Connection) -> None:
    """The merge fallback requires both source_id AND company_id to be set —
    a None company_id (e.g. unlinked SERP rows) takes the INSERT branch.
    """
    parsed_a = ParsedJob(
        title="Engineer A",
        company="UnlinkedCo",
        dedup_key=_dedup_key("UnlinkedCo", "Engineer A"),
        sources=["linkedin"],
        source_urls=["https://example.com/a"],
        source_id="external-id-1",
    )
    r1 = upsert_job(conn, parsed_a, company_id=None)
    assert r1.kind == "inserted"

    # Same source_id but no company_id — partial index exempts the row,
    # so INSERT proceeds.
    parsed_b = ParsedJob(
        title="Engineer B",
        company="UnlinkedCo",
        dedup_key=_dedup_key("UnlinkedCo", "Engineer B"),
        sources=["linkedin"],
        source_urls=["https://example.com/b"],
        source_id="external-id-1",
    )
    r2 = upsert_job(conn, parsed_b, company_id=None)
    assert r2.kind == "inserted"
