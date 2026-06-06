"""CI gate: verify the Job->ParsedJob shim is fully removed (Phase 48.07).

Two assertions:
  1. grep gate -- no call site in job_finder/ passes a raw Job() to upsert_job().
  2. runtime gate -- upsert_job(conn, Job(...)) raises TypeError.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# 1. grep gate -- zero matches of upsert_job(.*Job( in job_finder/
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JOB_FINDER_DIR = _REPO_ROOT / "job_finder"

# Pattern that the acceptance criteria check for.
# Matches: upsert_job(  followed by any chars followed by  Job(
_SHIM_PATTERN = re.compile(r"upsert_job\(.*Job\(")


def _source_lines_with_shim() -> list[tuple[Path, int, str]]:
    """Return (file, lineno, line) for every match in job_finder/ source."""
    hits: list[tuple[Path, int, str]] = []
    for py_file in _JOB_FINDER_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SHIM_PATTERN.search(line):
                hits.append((py_file, lineno, line.strip()))
    return hits


def test_no_raw_job_passed_to_upsert_job() -> None:
    """grep gate: no file in job_finder/ calls upsert_job(.*Job(.

    If this test fails, the listed lines still pass a raw Job object to
    upsert_job().  Fix them by calling ParsedJob.from_job(job) first and
    passing the result to upsert_job().
    """
    hits = _source_lines_with_shim()
    if hits:
        formatted = "\n".join(
            f"  {path.relative_to(_REPO_ROOT)}:{lineno}: {line}"
            for path, lineno, line in hits
        )
        pytest.fail(
            f"Found {len(hits)} call site(s) that still pass a raw Job to upsert_job():\n"
            f"{formatted}\n\n"
            "Replace each with:\n"
            "    parsed = ParsedJob.from_job(job, source_meta=...)\n"
            "    result = upsert_job(conn, parsed, ...)"
        )


# ---------------------------------------------------------------------------
# 2. runtime gate -- upsert_job(conn, Job(...)) raises TypeError
# ---------------------------------------------------------------------------


@pytest.fixture()
def _conn() -> Iterator[sqlite3.Connection]:
    # Use NamedTemporaryFile(delete=False) pattern — matches project convention
    # (test_upsert_job_contract.py) so Windows doesn't hold the file open.
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


def test_upsert_job_rejects_raw_job(_conn: sqlite3.Connection) -> None:
    """upsert_job() raises TypeError when passed a raw Job (not ParsedJob)."""
    job = Job(
        title="Software Engineer",
        company="TestCo",
        location="Remote",
        source="serpapi",
        source_url="https://example.com/jobs/1",
    )
    with pytest.raises(TypeError) as exc_info:
        upsert_job(_conn, job)  # type: ignore[arg-type]

    msg = str(exc_info.value)
    assert "ParsedJob" in msg, f"TypeError message should mention ParsedJob, got: {msg!r}"
    assert "Job" in msg, f"TypeError message should mention Job type, got: {msg!r}"
