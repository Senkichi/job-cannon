"""Public-surface + sort_by-allowlist sentinels for `job_finder.db` (S7d).

Two purposes:

1. **Public-surface sentinel.** Enumerates every name that downstream code (in
   the worktree at `97dbae1`) imports from `job_finder.db`. Any future split
   of `db.py` into a package MUST keep these names re-exported through
   `job_finder/db/__init__.py`. If a name disappears, this test fails BEFORE
   the dependent test files raise ImportError — so a `git bisect` lands on
   the offending commit instead of on a downstream test that imports the
   missing symbol incidentally.

2. **sort_by allowlist sentinel (SQL-injection guard).** CLAUDE.md documents
   the invariant: ``sort_by validated against Python allowlist before SQL
   interpolation (no parameterized column names in SQLite)``. This sentinel
   verifies BOTH that every advertised allowlist value works AND that a
   malicious sort_by string falls back safely without dropping the jobs
   table.

The sentinel is the bisectability proof for S7d's split: it MUST stay green
on every intermediate commit, not just the final one.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Sentinel A — public-surface enumeration
# ---------------------------------------------------------------------------

# Names defined directly in `job_finder/db.py` (pre-S7d). Each must remain
# importable from `job_finder.db` after any split — either because it lives
# in `db/__init__.py` directly, or because `__init__.py` re-exports it from
# a private sub-module.
_DB_DIRECT_SYMBOLS: tuple[str, ...] = (
    # Classification rule cluster
    "JobAssessment",
    "derive_classification",
    "_SUB_SCORE_KEYS",
    # Job CRUD
    "merge_description",
    "upsert_job",
    "get_job",
    "load_job_context",
    "JOBS_ALL_COLUMNS",
    # Persistence
    "log_run",
    "persist_job_assessment",
    "persist_job_expiry_state",
    "update_pipeline_status",
    # Read-only filter queries
    "get_distinct_sources",
    "get_filtered_jobs",
)

# Re-exports from sibling modules `db_pipeline.py` and `db_queries.py`. These
# remain re-exports through `db/__init__.py` after the split — the sibling
# files themselves are NOT moved into the new package.
_DB_REEXPORTED_SYMBOLS: tuple[str, ...] = (
    # from db_pipeline.py
    "get_pending_detections",
    "get_pipeline_events",
    "resolve_detection",
    # from db_queries.py
    "get_dashboard_stats",
    "get_recent_runs",
    "get_pipeline_summary",
    "get_jobs_by_status",
    "get_distinct_locations",
    "get_recent_activity",
    "get_recent_pipeline_events",
)


def test_db_module_imports_cleanly():
    """`import job_finder.db` succeeds — no top-level ImportError."""
    import job_finder.db

    assert job_finder.db is not None


@pytest.mark.parametrize("name", _DB_DIRECT_SYMBOLS)
def test_db_direct_symbol_is_importable(name: str):
    """Each direct-defined symbol resolves via `getattr(job_finder.db, X)`.

    Equivalent to `from job_finder.db import X` for the purposes of the
    package re-export contract — Python's import machinery resolves both
    against the module's namespace.
    """
    import job_finder.db as db

    assert hasattr(db, name), (
        f"job_finder.db is missing direct symbol {name!r} — likely a regression "
        "from a refactor that moved it without preserving the package re-export."
    )
    assert getattr(db, name) is not None


@pytest.mark.parametrize("name", _DB_REEXPORTED_SYMBOLS)
def test_db_reexported_symbol_is_importable(name: str):
    """Each sibling-module re-export resolves via `from job_finder.db import X`."""
    import job_finder.db as db

    assert hasattr(db, name), (
        f"job_finder.db is missing re-exported symbol {name!r} — the package "
        "must continue to forward names from db_pipeline.py and db_queries.py."
    )
    assert getattr(db, name) is not None


# ---------------------------------------------------------------------------
# Sentinel B — sort_by allowlist invariant (SQL-injection guard)
# ---------------------------------------------------------------------------
#
# The allowlist documented in `get_filtered_jobs` (db.py:705 at S7e-close):
#
#     allowed_sort_cols = {
#         "score", "title", "company", "location", "first_seen",
#         "salary_min", "salary_max", "pipeline_status",
#     } | _CLASSIFICATION_SORT_KEYS  # adds: classification, classification_rank, sub_score_sum
#
# After S7d, this allowlist's lexical scope MUST stay co-located with the
# f-string composer that consumes it (no allowlist-in-constants /
# composer-in-function-body split).

_DOCUMENTED_ALLOWLIST: tuple[str, ...] = (
    "score",
    "title",
    "company",
    "location",
    "first_seen",
    "salary_min",
    "salary_max",
    "pipeline_status",
    "classification",
    "classification_rank",
    "sub_score_sum",
)


@pytest.fixture
def migrated_conn():
    """Temp DB at the latest migration. Same shape as tests/test_db.py's fixture
    so the sentinel exercises pipeline_events FK + every column that
    get_filtered_jobs touches.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.parametrize("sort_by", _DOCUMENTED_ALLOWLIST)
def test_allowlisted_sort_by_does_not_raise(migrated_conn, sort_by: str):
    """Every documented allowlist value round-trips through get_filtered_jobs.

    If a future refactor accidentally drops a key from `_CLASSIFICATION_SORT_KEYS`
    or from the inline allowlist, this fails by surfacing a SQL-error
    OperationalError (or, for the silent-fallback path, by `sort_by` no longer
    matching reality — which the next test catches).
    """
    from job_finder.db import get_filtered_jobs

    # Empty DB — assertion is "did NOT raise"; result list is allowed to be empty.
    result = get_filtered_jobs(migrated_conn, sort_by=sort_by, limit=1)
    assert isinstance(result, list)


def test_malicious_sort_by_does_not_drop_table(migrated_conn):
    """A SQL-injection-style sort_by must NOT drop the jobs table.

    The documented behavior (CLAUDE.md + db.py:715–716) is silent fallback to
    the safe default — the function does NOT raise on unknown sort_by; it
    sets `sort_by = "score"` and continues. This test verifies that path
    survives a deliberately malicious value.
    """
    from job_finder.db import get_filtered_jobs

    pre_jobs_table = migrated_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()[0]
    assert pre_jobs_table == 1, "sentinel precondition: migrated DB has jobs table"

    # The classic injection attempt — appending a DROP via a literal string.
    # Per the allowlist guard, this is rejected silently (sort_by reset to
    # 'score') and the query proceeds normally.
    result = get_filtered_jobs(
        migrated_conn,
        sort_by="id; DROP TABLE jobs; --",
        limit=1,
    )

    # Functional assertion: query returned a list (no exception).
    assert isinstance(result, list)

    # Security assertion: jobs table is still present.
    post_jobs_table = migrated_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()[0]
    assert post_jobs_table == 1, (
        "SECURITY REGRESSION: a malicious sort_by string dropped the jobs "
        "table. The allowlist guard at the entry of get_filtered_jobs MUST "
        "remain co-located with the f-string composer that interpolates "
        "sort_by into ORDER BY."
    )


def test_unknown_sort_by_falls_back_silently(migrated_conn):
    """A non-malicious unknown sort_by also falls back without raising.

    This is the wider behavior contract (every value not in the allowlist is
    silently rewritten to 'score'). Worth pinning so a future refactor that
    converts this path to `raise ValueError(...)` has to do so deliberately,
    not accidentally.
    """
    from job_finder.db import get_filtered_jobs

    result = get_filtered_jobs(
        migrated_conn,
        sort_by="this_column_definitely_does_not_exist",
        limit=1,
    )
    assert isinstance(result, list)
