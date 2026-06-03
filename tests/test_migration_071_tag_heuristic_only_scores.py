"""Tests for Migration 71 — tag heuristic-only-scored rows.

Covers:
- score set + provider NULL → tagged 'heuristic'.
- score set + provider already 'ollama' (LLM ran) → untouched.
- score NULL + provider NULL (neither ran) → untouched.
- score NULL + provider 'ollama' (impossible-but-defensive) → untouched.
- Idempotent re-run (no double-tag).
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.migrations.m071_tag_heuristic_only_scores import MIGRATION, _tag
from job_finder.web.migrations.types import MigrationContext
from tests.helpers.contract_triggers import (
    run_migrations_without_contract as run_migrations,
)


@pytest.fixture
def migrated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
    score: float | None,
    provider: str | None,
) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               pipeline_status, sources, score, scoring_provider,
               first_seen, last_seen)
            VALUES (?, 'T', 'C', 'X', '[]',
                    'discovered', '["test"]', ?, ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, score, provider),
    )
    conn.commit()


def _read_provider(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    r = conn.execute(
        "SELECT scoring_provider FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return r["scoring_provider"]


def _run(path: str, conn: sqlite3.Connection) -> None:
    _tag(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=70))
    conn.commit()


def test_migration_declares_version_71():
    assert MIGRATION.version == 71


class TestTags:
    def test_score_set_provider_null_gets_heuristic_tag(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="heuristic|a", score=72.5, provider=None)
        _run(path, conn)
        assert _read_provider(conn, "heuristic|a") == "heuristic"

    def test_zero_score_still_tagged(self, migrated_db):
        # heuristic can produce 0.0 (title-exclusion hit), and 0.0 is a
        # real score, not "no score" — must still be tagged.
        path, conn = migrated_db
        _insert(conn, dedup_key="heuristic|b", score=0.0, provider=None)
        _run(path, conn)
        assert _read_provider(conn, "heuristic|b") == "heuristic"


class TestPreserves:
    def test_llm_provider_untouched(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="llm|c", score=72.5, provider="ollama")
        _run(path, conn)
        assert _read_provider(conn, "llm|c") == "ollama"

    def test_no_score_no_provider_untouched(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="none|d", score=None, provider=None)
        _run(path, conn)
        assert _read_provider(conn, "none|d") is None

    def test_no_score_with_provider_untouched(self, migrated_db):
        # Defensive: a degenerate "provider set but score NULL" row should
        # not change either way.
        path, conn = migrated_db
        _insert(conn, dedup_key="weird|e", score=None, provider="ollama")
        _run(path, conn)
        assert _read_provider(conn, "weird|e") == "ollama"


class TestIdempotence:
    def test_second_run_no_double_tag(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="heuristic|f", score=50.0, provider=None)
        _run(path, conn)
        first = _read_provider(conn, "heuristic|f")
        _run(path, conn)
        assert _read_provider(conn, "heuristic|f") == first == "heuristic"


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0
