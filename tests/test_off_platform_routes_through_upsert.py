"""Off-platform email stubs route through upsert_job (Phase 47.09 / D-15).

The off-platform stub creator (`_try_create_stub_job`) previously issued a raw
`INSERT INTO jobs`, bypassing the typed contract. It now constructs a ParsedJob
and calls upsert_job. This test verifies the stub still lands with the expected
shape AND that no `INSERT INTO jobs` survives anywhere outside `_jobs.py` /
`migrations/` (the structural defense against re-introducing the bypass —
mirrors Phase 46.03's writer-routing gates).
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.pipeline_detector._off_platform import _try_create_stub_job

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[1] / "job_finder"
_INSERT_RE = re.compile(r"INSERT\s+INTO\s+jobs\b", re.IGNORECASE)


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


# ---------------------------------------------------------------------------
# Behavior — the stub lands with the contract-conformant shape
# ---------------------------------------------------------------------------


def test_off_platform_stub_created_via_upsert(conn: sqlite3.Connection):
    result = _try_create_stub_job({"from_address": "careers@newcorp.com"}, conn)
    assert result is not None
    assert result["attributed_existing"] is False

    row = conn.execute(
        "SELECT pipeline_status, sources, scoring_provider, company, title "
        "FROM jobs WHERE dedup_key = ?",
        (result["dedup_key"],),
    ).fetchone()
    assert row is not None
    assert row["pipeline_status"] == "discovered"
    assert row["sources"] == '["off_platform_email"]'
    # upsert_job's INSERT path tags heuristic (was DB-default before); either way
    # scoring_provider is non-NULL so the m078 I-03 trigger is satisfied.
    assert row["scoring_provider"] == "heuristic"


def test_off_platform_stub_synthetic_dedup_key_shape(conn: sqlite3.Connection):
    result = _try_create_stub_job({"from_address": "jobs@acme.io"}, conn)
    assert result is not None
    # f"{candidate.lower()}|off-platform|{ms_timestamp}"
    parts = result["dedup_key"].split("|")
    assert parts[1] == "off-platform"
    assert parts[2].isdigit()


# ---------------------------------------------------------------------------
# CI grep gate — no raw INSERT INTO jobs outside _jobs.py / migrations
# ---------------------------------------------------------------------------


def test_no_insert_into_jobs_outside_upsert():
    offenders: list[str] = []
    for py in _JOB_FINDER_ROOT.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if py.name == "_jobs.py" or "migrations" in py.parts:
            continue
        if _INSERT_RE.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(_JOB_FINDER_ROOT)))
    assert not offenders, (
        "INSERT INTO jobs found outside job_finder/db/_jobs.py and migrations/: "
        f"{offenders}. Route the write through upsert_job (D-15)."
    )
