"""Tests for autoheal ATS/careers capture convention (Task 6).

These tests pin the naming convention the hooks must follow, and verify
the capture-only (detect=False) path never auto-degrades.
"""

import sqlite3

from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.db_migrate import run_migrations


def _conn(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return db, conn


def test_ats_source_naming_is_per_platform(tmp_path):
    _, conn = _conn(tmp_path)
    hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=3)
    row = conn.execute(
        "SELECT surface FROM source_health WHERE source='ats:greenhouse'"
    ).fetchone()
    assert row["surface"] == "ats"


def test_detect_false_never_auto_degrades(tmp_path):
    """ATS/careers capture path: baseline recorded but break counter stays frozen."""
    db, conn = _conn(tmp_path)
    # Establish a baseline first
    for _ in range(3):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=5, detect=False)
    # Feed many zero-yield meaningful inputs
    for _ in range(BREAK_THRESHOLD + 2):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=0, detect=False)
    row = conn.execute(
        "SELECT status, consecutive_breaks, baseline_yield FROM source_health "
        "WHERE source='ats:greenhouse'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["consecutive_breaks"] == 0
    assert row["baseline_yield"] == 5.0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM corpus_sample WHERE source='ats:greenhouse'"
        ).fetchone()[0]
        > 0
    )


def test_careers_source_naming(tmp_path):
    _, conn = _conn(tmp_path)
    hm.record_extraction(
        conn, "careers", "careers", "tier=static found=2", job_count=2, detect=False
    )
    row = conn.execute("SELECT surface FROM source_health WHERE source='careers'").fetchone()
    assert row["surface"] == "careers"
