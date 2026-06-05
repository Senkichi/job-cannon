"""CI gate: Phase 48.07 shim removal.

Verifies:
  1. No call site in job_finder/ passes a raw Job to upsert_job
     (grep gate: ``upsert_job(.*Job(`` must return zero matches).
  2. Calling upsert_job(conn, Job(...)) raises TypeError at runtime
     (the explicit type guard added in Phase 48.07).

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md §12 commit 48.07
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115
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
# 1. Grep gate — no upsert_job(.*Job( in job_finder/
# ---------------------------------------------------------------------------


def test_no_upsert_job_with_raw_job_in_source() -> None:
    """grep -rn 'upsert_job(.*Job(' job_finder/ must return no matches.

    This is the acceptance-criteria CI gate for Phase 48.07.
    Any match outside tests/ means a caller was not migrated.
    """
    pattern = re.compile(r"upsert_job\(.*Job\(")

    repo_root = Path(__file__).parent.parent
    job_finder_dir = repo_root / "job_finder"

    matches: list[str] = []
    for py_file in sorted(job_finder_dir.rglob("*.py")):
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                rel = py_file.relative_to(repo_root)
                matches.append(f"{rel}:{lineno}: {line.strip()}")

    assert matches == [], (
        "Found upsert_job(.*Job( in job_finder/ — shim not fully removed:\n"
        + "\n".join(matches)
    )


# ---------------------------------------------------------------------------
# 2. Runtime TypeError guard — upsert_job rejects raw Job objects
# ---------------------------------------------------------------------------


def test_upsert_job_raises_type_error_for_raw_job(conn: sqlite3.Connection) -> None:
    """upsert_job(conn, Job(...)) must raise TypeError (Phase 48.07 type guard)."""
    job = Job(
        title="Shim Removed Engineer",
        company="ShimRemovedCo",
        location="Remote",
        source="test",
        source_url="https://example.com/jobs/shim-removed",
    )
    with pytest.raises(TypeError, match="ParsedJob"):
        upsert_job(conn, job)  # type: ignore[arg-type]
