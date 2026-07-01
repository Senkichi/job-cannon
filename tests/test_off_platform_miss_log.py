"""Off-platform miss log with reachability classification (Issue #591).

Tests the get_off_platform_miss_log function that classifies off-platform jobs
by reachability into four buckets: reachable, unreachable_untracked,
unreachable_unsupported, and unreachable_scan_disabled.

Seeding goes through the REAL write paths on purpose:
  - upsert_job() writes the RAW parser name into jobs.company and leaves
    jobs.company_id NULL (exactly as ingestion does).
  - upsert_company() writes the NORMALIZED name into companies.name (raw kept
    only in companies.name_raw).
Because companies.name is normalized while jobs.company is raw, a name-based
join (jobs.company = companies.name) would miss nearly every real link and
invert the metric. The reachability linkage is the jobs.company_id FK, which
production populates via company_resolver.link_jobs_to_companies; the tests
populate it explicitly to the id upsert_company returns. test_raw_vs_normalized_
name_still_reachable pins that boundary directly.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import get_off_platform_miss_log, upsert_job
from job_finder.parsed_job import ParsedJob
from job_finder.web.ats_company import upsert_company
from job_finder.web.db_migrate import run_migrations

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        yield c
        c.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def _seed_off_platform_stub(
    conn: sqlite3.Connection,
    company: str,
    dedup_key: str,
    company_id: int | None = None,
) -> None:
    """Seed an off-platform stub job via the real upsert_job path.

    Optionally link jobs.company_id to a seeded company, mirroring what
    company_resolver.link_jobs_to_companies does in production. Left NULL when
    no company_id is supplied (the untracked case).
    """
    parsed = ParsedJob(
        title="(off-platform — title TBD)",
        company=company,
        dedup_key=dedup_key,
        location="",
        sources=["off_platform_email"],
        source_urls=[],
    )
    upsert_job(conn, parsed)
    if company_id is not None:
        conn.execute(
            "UPDATE jobs SET company_id = ? WHERE dedup_key = ?",
            (company_id, dedup_key),
        )
        conn.commit()


def _seed_normal_job(
    conn: sqlite3.Connection,
    company: str,
    dedup_key: str,
) -> None:
    """Seed a normal job (not off-platform)."""
    parsed = ParsedJob(
        title="Software Engineer",
        company=company,
        dedup_key=dedup_key,
        location="Remote",
        sources=["linkedin"],
        source_urls=["https://linkedin.com/jobs/123"],
    )
    upsert_job(conn, parsed)


def _seed_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: str | None,
    scan_enabled: int = 1,
) -> int:
    """Seed a company through the REAL write path (upsert_company).

    upsert_company normalizes the name into companies.name (so it differs from
    the raw jobs.company), preserving the raw form only in name_raw — the exact
    production state that a raw-name join would fail against. Returns the
    company_id so the caller can link a job's company_id FK to it.
    """
    cid = upsert_company(conn, name, ats_platform=ats_platform, ats_probe_status="hit")
    assert cid is not None, f"upsert_company unexpectedly rejected {name!r}"
    if scan_enabled != 1:
        conn.execute("UPDATE companies SET scan_enabled = ? WHERE id = ?", (scan_enabled, cid))
        conn.commit()
    return cid


# ---------------------------------------------------------------------------
# Reachability bucket tests
# ---------------------------------------------------------------------------


def test_reachable_bucket(conn: sqlite3.Connection):
    """Off-platform stub with tracked company, scannable ATS, scan_enabled=1 -> reachable."""
    cid = _seed_company(conn, "Acme Corp", "greenhouse", scan_enabled=1)
    _seed_off_platform_stub(conn, "Acme Corp", "acme-corp|off-platform|1000", company_id=cid)

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 1
    assert result["reachable"] == 1
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 0
    assert result["unreachable_scan_disabled"] == 0
    assert len(result["cases"]) == 1
    assert result["cases"][0]["bucket"] == "reachable"


def test_raw_vs_normalized_name_still_reachable(conn: sqlite3.Connection):
    """Regression for the raw-vs-normalized join bug: a company whose raw name
    differs from its normalized companies.name (case + legal suffix) must still
    bucket as reachable via the company_id FK. A name-join (jobs.company =
    companies.name) would have missed this link and mislabeled it untracked.
    """
    # "Toast, Inc." normalizes to something like "toast" — raw != normalized.
    cid = _seed_company(conn, "Toast, Inc.", "lever", scan_enabled=1)
    # Sanity: the stored normalized name is NOT the raw name (else the test would
    # not actually exercise the boundary).
    stored_name = conn.execute("SELECT name FROM companies WHERE id = ?", (cid,)).fetchone()[0]
    assert stored_name != "Toast, Inc.", "expected upsert_company to normalize the name"

    _seed_off_platform_stub(conn, "Toast, Inc.", "toast|off-platform|1000", company_id=cid)

    result = get_off_platform_miss_log(conn)
    assert result["reachable"] == 1
    assert result["unreachable_untracked"] == 0
    assert result["cases"][0]["bucket"] == "reachable"


def test_untracked_bucket(conn: sqlite3.Connection):
    """Off-platform stub with no linked company (company_id NULL) -> unreachable_untracked."""
    _seed_off_platform_stub(conn, "Unknown Corp", "unknown-corp|off-platform|1000")

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 1
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 1
    assert result["unreachable_unsupported"] == 0
    assert result["unreachable_scan_disabled"] == 0
    assert len(result["cases"]) == 1
    assert result["cases"][0]["bucket"] == "unreachable_untracked"


def test_unsupported_platform_bucket(conn: sqlite3.Connection):
    """Tracked company with non-scannable ATS -> unreachable_unsupported."""
    cid = _seed_company(conn, "Legacy Corp", "custom_ats", scan_enabled=1)
    _seed_off_platform_stub(conn, "Legacy Corp", "legacy-corp|off-platform|1000", company_id=cid)

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 1
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 1
    assert result["unreachable_scan_disabled"] == 0
    assert len(result["cases"]) == 1
    assert result["cases"][0]["bucket"] == "unreachable_unsupported"


def test_unsupported_platform_null(conn: sqlite3.Connection):
    """Tracked company with NULL ats_platform -> unreachable_unsupported."""
    cid = _seed_company(conn, "No ATS Corp", None, scan_enabled=1)
    _seed_off_platform_stub(conn, "No ATS Corp", "no-ats-corp|off-platform|1000", company_id=cid)

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 1
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 1
    assert result["unreachable_scan_disabled"] == 0
    assert len(result["cases"]) == 1
    assert result["cases"][0]["bucket"] == "unreachable_unsupported"


def test_scan_disabled_bucket(conn: sqlite3.Connection):
    """Tracked, scannable ATS, but scan_enabled=0 -> unreachable_scan_disabled."""
    cid = _seed_company(conn, "Disabled Corp", "greenhouse", scan_enabled=0)
    _seed_off_platform_stub(
        conn, "Disabled Corp", "disabled-corp|off-platform|1000", company_id=cid
    )

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 1
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 0
    assert result["unreachable_scan_disabled"] == 1
    assert len(result["cases"]) == 1
    assert result["cases"][0]["bucket"] == "unreachable_scan_disabled"


def test_scan_enabled_null_counts_as_reachable(conn: sqlite3.Connection):
    """scan_enabled NULL (unknown) is NOT a scope decision — the schema default is
    enabled, so a tracked + scannable company with NULL scan_enabled buckets as
    reachable, not unreachable_scan_disabled."""
    cid = _seed_company(conn, "Nullscan Corp", "greenhouse", scan_enabled=1)
    conn.execute("UPDATE companies SET scan_enabled = NULL WHERE id = ?", (cid,))
    conn.commit()
    _seed_off_platform_stub(
        conn, "Nullscan Corp", "nullscan-corp|off-platform|1000", company_id=cid
    )

    result = get_off_platform_miss_log(conn)

    assert result["reachable"] == 1
    assert result["unreachable_scan_disabled"] == 0
    assert result["cases"][0]["bucket"] == "reachable"


def test_buckets_sum_to_total(conn: sqlite3.Connection):
    """Mixed seed; assert the four buckets sum to total."""
    acme = _seed_company(conn, "Acme Corp", "greenhouse", scan_enabled=1)
    legacy = _seed_company(conn, "Legacy Corp", "custom_ats", scan_enabled=1)
    disabled = _seed_company(conn, "Disabled Corp", "greenhouse", scan_enabled=0)

    _seed_off_platform_stub(conn, "Acme Corp", "acme-corp|off-platform|1000", company_id=acme)
    _seed_off_platform_stub(conn, "Unknown Corp", "unknown-corp|off-platform|2000")  # unlinked
    _seed_off_platform_stub(
        conn, "Legacy Corp", "legacy-corp|off-platform|3000", company_id=legacy
    )
    _seed_off_platform_stub(
        conn, "Disabled Corp", "disabled-corp|off-platform|4000", company_id=disabled
    )

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 4
    assert result["reachable"] == 1
    assert result["unreachable_untracked"] == 1
    assert result["unreachable_unsupported"] == 1
    assert result["unreachable_scan_disabled"] == 1
    assert (
        result["reachable"]
        + result["unreachable_untracked"]
        + result["unreachable_unsupported"]
        + result["unreachable_scan_disabled"]
        == result["total"]
    )


def test_non_off_platform_jobs_excluded(conn: sqlite3.Connection):
    """Normal discovered job with sources=['linkedin'] is absent from every bucket."""
    _seed_normal_job(conn, "Normal Corp", "normal-corp|linkedin|1000")

    result = get_off_platform_miss_log(conn)

    assert result["total"] == 0
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 0
    assert result["unreachable_scan_disabled"] == 0
    assert len(result["cases"]) == 0


def _make_target_member(
    conn: sqlite3.Connection,
    dedup_key: str,
    mean_score: int = 4,
    classification: str = "apply",
) -> None:
    """Promote a seeded job to a scored target-set member: all six sub-scores set to
    ``mean_score`` (so the mean is exactly mean_score) with a non-hard-negative band."""
    subs = (
        f'{{"title_fit":{mean_score},"location_fit":{mean_score},"comp_fit":{mean_score},'
        f'"domain_match":{mean_score},"seniority_match":{mean_score},"skills_match":{mean_score}}}'
    )
    conn.execute(
        "UPDATE jobs SET sub_scores_json = ?, classification = ?, scoring_model = 'qwen2.5:14b' "
        "WHERE dedup_key = ?",
        (subs, classification, dedup_key),
    )


def test_fit_floor_none_when_not_supplied(conn: sqlite3.Connection):
    """reachable_above_fit_floor is None when the caller passes no fit_floor — the field
    degrades to None rather than silently reporting a wrong count. The four core buckets
    are still computed."""
    cid = _seed_company(conn, "Acme Corp", "greenhouse", scan_enabled=1)
    _seed_off_platform_stub(conn, "Acme Corp", "acme-corp|off-platform|1000", company_id=cid)

    result = get_off_platform_miss_log(conn)  # no fit_floor supplied

    assert result["total"] == 1
    assert result["reachable"] == 1
    assert result["reachable_above_fit_floor"] is None


def test_fit_floor_counts_reachable_target_members(conn: sqlite3.Connection):
    """When fit_floor IS supplied, reachable_above_fit_floor counts only the reachable
    misses that are target-set members (scored, mean >= floor, not a hard negative).

    Regression guard: the original impl called get_fit_floor() with no args (its real
    signature requires `config`), silently TypeError'd inside a broad except, and left
    this field permanently None — a dead sub-feature that the old test pinned as None.
    """
    # Reachable AND a target-set member (mean 4.0 >= 3.5).
    acme = _seed_company(conn, "Acme Corp", "greenhouse", scan_enabled=1)
    _seed_off_platform_stub(conn, "Acme Corp", "acme-corp|off-platform|1000", company_id=acme)
    _make_target_member(conn, "acme-corp|off-platform|1000", mean_score=4)

    # Reachable but BELOW the fit-floor (mean 2.0) — must NOT be counted.
    beta = _seed_company(conn, "Beta Corp", "lever", scan_enabled=1)
    _seed_off_platform_stub(conn, "Beta Corp", "beta-corp|off-platform|1000", company_id=beta)
    _make_target_member(conn, "beta-corp|off-platform|1000", mean_score=2)

    result = get_off_platform_miss_log(conn, fit_floor=3.5)

    assert result["reachable"] == 2  # both companies are scannable → reachable
    assert result["reachable_above_fit_floor"] == 1  # but only Acme clears the fit-floor


def test_missing_companies_table_degrades(conn: sqlite3.Connection):
    """Drop companies table; assert function returns zeros instead of raising."""
    _seed_off_platform_stub(conn, "Unknown Corp", "unknown-corp|off-platform|1000")

    # Drop the companies table to simulate pre-migration state
    conn.execute("DROP TABLE IF EXISTS companies")

    result = get_off_platform_miss_log(conn)

    # Should return zeroed dict instead of raising
    assert result["total"] == 0
    assert result["reachable"] == 0
    assert result["unreachable_untracked"] == 0
    assert result["unreachable_unsupported"] == 0
    assert result["unreachable_scan_disabled"] == 0
    assert result["reachable_above_fit_floor"] is None
    assert len(result["cases"]) == 0
