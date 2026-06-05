"""upsert_job salary normalization at the persistence boundary.

The legacy contract was that ``salary_min`` and ``salary_max`` were
written as-is by every parser. Several sources (DataForSEO short
numerics, Workday blob extracts, a Glassdoor unit-confusion bug) had
landed rows where ``salary_min > salary_max`` — 9 such rows existed at
the 2026-05-28 audit. The fix wires ``_normalize_salary`` into both
the INSERT and UPDATE branches so no new row can land in that state.

Policy mirrors m069 (the heal migration for existing rows):

  - Both NULL or only one set: pass through.
  - min <= max: pass through.
  - min > max, ratio <= 10x after swap: swap (parser emitted reversed).
  - min > max, ratio > 10x after swap: null both (unit mismatch — can't
    trust either value, m062 may recover later from jd_full).
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.parsed_job import ParsedJob
from job_finder.db._jobs import _normalize_salary
from job_finder.models import Job
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


def _make_job(*, smin: int | None, smax: int | None, title: str = "Senior Eng") -> Job:
    return Job(
        title=title,
        company="TestCo",
        location="San Francisco, CA",
        source="lever",
        source_url=f"https://example.com/j/{title}",
        description="x" * 250,
        salary_min=smin,
        salary_max=smax,
    )


def _make_parsed(*, smin: int | None, smax: int | None, title: str = "Senior Eng") -> ParsedJob:
    return ParsedJob.from_job(_make_job(smin=smin, smax=smax, title=title))


def _read_salary(conn: sqlite3.Connection, dedup_key: str) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return (row["salary_min"], row["salary_max"])


# ---------- Pure function ----------


class TestNormalizeSalaryPure:
    def test_both_none_passthrough(self):
        assert _normalize_salary(None, None) == (None, None)

    def test_only_min_passthrough(self):
        assert _normalize_salary(100_000, None) == (100_000, None)

    def test_only_max_passthrough(self):
        assert _normalize_salary(None, 150_000) == (None, 150_000)

    def test_well_ordered_passthrough(self):
        assert _normalize_salary(100_000, 150_000) == (100_000, 150_000)

    def test_equal_passthrough(self):
        assert _normalize_salary(100_000, 100_000) == (100_000, 100_000)

    def test_simple_inversion_swapped(self):
        # xAI bug: $62-75/hr emitted as (75, 62). Ratio 75/62 = 1.2, well
        # within the 10x band — assume reversed-order, swap.
        assert _normalize_salary(75, 62) == (62, 75)

    def test_workday_inversion_swapped(self):
        # JLL: 100000/90309 — same unit, just reversed.
        assert _normalize_salary(100_000, 90_309) == (90_309, 100_000)

    def test_extreme_inversion_nulled(self):
        # PG&E Glassdoor bug: 140000/2000 — almost certainly an annual/
        # hourly unit mash-up; swap would give $2k - $140k which is just
        # a more-subtle-but-still-wrong row. Null both.
        assert _normalize_salary(140_000, 2_000) == (None, None)

    def test_extreme_inversion_nulled_at_boundary(self):
        # Ratio of exactly 10:1 should be allowed (swap), > 10:1 should null.
        assert _normalize_salary(100, 10) == (10, 100)  # 10:1 ratio after swap
        assert _normalize_salary(101, 10) == (None, None)  # > 10:1

    def test_zero_max_nulled(self):
        # max=0 is degenerate; can't divide by zero in the ratio test.
        assert _normalize_salary(100_000, 0) == (None, None)

    def test_negative_max_nulled(self):
        assert _normalize_salary(100_000, -5_000) == (None, None)


# ---------- INSERT path ----------


class TestUpsertInsertNormalizes:
    def test_insert_inverted_swaps(self, conn: sqlite3.Connection):
        upsert_job(conn, _make_parsed(smin=75, smax=62, title="a"))
        assert _read_salary(conn, "testco|a") == (62, 75)

    def test_insert_extreme_inversion_nulls_both(self, conn: sqlite3.Connection):
        upsert_job(conn, _make_parsed(smin=140_000, smax=2_000, title="b"))
        assert _read_salary(conn, "testco|b") == (None, None)

    def test_insert_well_ordered_unchanged(self, conn: sqlite3.Connection):
        upsert_job(conn, _make_parsed(smin=120_000, smax=150_000, title="c"))
        assert _read_salary(conn, "testco|c") == (120_000, 150_000)

    def test_insert_only_one_side_preserved(self, conn: sqlite3.Connection):
        upsert_job(conn, _make_parsed(smin=120_000, smax=None, title="d"))
        assert _read_salary(conn, "testco|d") == (120_000, None)


# ---------- UPDATE path ----------


class TestUpsertUpdateNormalizes:
    def test_update_inverted_swaps(self, conn: sqlite3.Connection):
        upsert_job(conn, _make_parsed(smin=100_000, smax=150_000, title="e"))
        # Re-upsert with inverted values (simulates a second source
        # re-asserting the same job with a buggy parse).
        upsert_job(conn, _make_parsed(smin=78, smax=68, title="e"))
        assert _read_salary(conn, "testco|e") == (68, 78)

    def test_update_extreme_inversion_keeps_existing(self, conn: sqlite3.Connection):
        # COALESCE pattern in UPDATE means: when normalization returns
        # (None, None), the existing values are preserved. This is the
        # safe behavior — don't overwrite a good range with the nulls.
        upsert_job(conn, _make_parsed(smin=120_000, smax=150_000, title="f"))
        upsert_job(conn, _make_parsed(smin=140_000, smax=2_000, title="f"))
        assert _read_salary(conn, "testco|f") == (120_000, 150_000)
