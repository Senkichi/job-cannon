"""Migration 97 — guarded backfill that dismisses historical aggregator rows (#213).

Verifies the state guard: only ``pipeline_status='discovered'`` rows whose
normalized company is a seeded aggregator transition to ``'dismissed'``. Rows the
user has touched (applied / reviewing / archived / already dismissed) and legit
companies are never modified. Suffix variants must still match.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m097_dismiss_aggregator_reposters import _heal
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115
    tmp.close()
    path = Path(tmp.name)
    try:
        run_migrations(str(path))
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        yield c, str(path)
        c.close()
    finally:
        path.unlink(missing_ok=True)


def _insert(conn: sqlite3.Connection, dedup_key: str, company: str, status: str) -> None:
    """Insert a minimal job row, bypassing contract triggers for legacy shapes."""
    from tests.helpers.contract_triggers import drop_contract_triggers

    drop_contract_triggers(conn)
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, source_urls, sources,
                pipeline_status, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Senior Data Scientist",
            company,
            "Remote",
            f"https://example.com/{dedup_key}",
            "test",
            status,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()


def _status(conn: sqlite3.Connection, dedup_key: str) -> str:
    return conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()[0]


def _run(conn_path) -> None:
    conn, path = conn_path
    _heal(MigrationContext(conn=conn, db_path=path, user_data_root="."))
    conn.commit()


def test_discovered_aggregator_suffix_variant_dismissed(conn):
    c, _ = conn
    _insert(c, "vv-inc", "Virtual Vocations Inc", "discovered")
    _run(conn)
    assert _status(c, "vv-inc") == "dismissed"


def test_all_seeded_aggregators_dismissed(conn):
    c, _ = conn
    cases = {
        "vv": "Virtual Vocations",
        "ps": "ProSidian Consulting, LLC",
        "si": "SynergisticIT",
        "si2": "Synergistic it",
    }
    for key, company in cases.items():
        _insert(c, key, company, "discovered")
    _run(conn)
    for key in cases:
        assert _status(c, key) == "dismissed", key


def test_legit_company_untouched(conn):
    c, _ = conn
    _insert(c, "legit", "Wise", "discovered")
    _run(conn)
    assert _status(c, "legit") == "discovered"


@pytest.mark.parametrize("protected_status", ["applied", "reviewing", "archived", "dismissed"])
def test_non_discovered_state_preserved(conn, protected_status):
    """The guard must never overwrite a user-touched pipeline state."""
    c, _ = conn
    _insert(c, "touched", "Virtual Vocations Inc", protected_status)
    _run(conn)
    assert _status(c, "touched") == protected_status


def test_idempotent_rerun(conn):
    c, _ = conn
    _insert(c, "vv", "Virtual Vocations Inc", "discovered")
    _run(conn)
    first = _status(c, "vv")
    _run(conn)  # second run must be a no-op
    assert _status(c, "vv") == first == "dismissed"


def test_runs_clean_via_full_migration_driver(conn):
    """run_migrations applied m097 without error and left version >= 97."""
    c, _ = conn
    version = c.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 97
