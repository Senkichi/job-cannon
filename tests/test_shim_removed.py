"""Phase 48.07 — CI gate: verify upsert_job shim is fully removed.

Two acceptance criteria:
  1. Programmatic grep: no ``upsert_job(.*Job(`` call sites remain in
     ``job_finder/`` outside test files.
  2. Runtime type check: ``upsert_job(conn, Job(...))`` raises ``TypeError``.

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
# 1. Grep gate — no upsert_job(.*Job( in job_finder/ (excluding tests/)
# ---------------------------------------------------------------------------


def test_no_upsert_job_with_raw_job_in_source() -> None:
    """grep -rn 'upsert_job(.*Job(' job_finder/ returns no matches.

    This is the acceptance-criteria CI gate from Phase 48.07 (GitHub #58).
    Fails if any non-test source file passes a raw Job to upsert_job.
    """
    repo_root = Path(__file__).parent.parent
    source_dir = repo_root / "job_finder"

    pattern = re.compile(r"upsert_job\(.*Job\(")

    violations: list[str] = []
    for py_file in source_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{py_file.relative_to(repo_root)}:{lineno}: {line.rstrip()}")

    assert violations == [], (
        "Found upsert_job(.*Job( call sites in job_finder/ — "
        "Phase 48.07 shim removal is incomplete:\n" + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# 2. Runtime rejection — upsert_job(conn, Job(...)) raises TypeError
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():  # type: ignore[return]
    """Provide a migrated SQLite connection (file-backed so run_migrations works)."""
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


def test_upsert_job_rejects_raw_job(conn: sqlite3.Connection) -> None:
    """upsert_job(conn, Job(...)) raises TypeError — shim is gone."""
    job = Job(
        title="Software Engineer",
        company="ShimTestCo",
        location="Remote",
        source="test",
        source_url="https://example.com/jobs/1",
    )
    with pytest.raises(TypeError, match="ParsedJob or UnresolvedParsedJob"):
        upsert_job(conn, job)  # type: ignore[arg-type]
