"""Tests for Migration 100 + the standing dedup re-key hook (P4.1, issue #377).

Covers (D-8: derived values are versioned):
- m100 creates schema_meta and seeds dedup_normalizer_version from the legacy
  once-ever sentinel state ('1' when the migration_complete sentinel exists,
  else '0').
- _run_rekey_if_stale runs run_retroactive_dedup when the stored version differs
  from NORMALIZER_VERSION, stamps the watermark, and is a no-op when they match.
- A populated DB with stale-key duplicates gets merged before/after; distinct
  jobs are never merged; merged canonicals are queued for re-score (NULLed).
- Bumping a (monkeypatched) version constant re-arms the hook.
- Fresh/empty DB: m100 is a no-op beyond seeding; the hook merges nothing.
- run_migrations on a fully fresh DB ends with the watermark at the current
  version and no spurious merges.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.normalizers import NORMALIZER_VERSION
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import _post_hooks


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
        os.unlink(path)


def _insert_job(conn, dedup_key, title, company, first_seen, **cols):
    base = {
        "location": "",
        "sources": "[]",
        "source_urls": "[]",
        "source_id": "",
        "pipeline_status": "discovered",
        "notes": "",
        "classification": None,
        "sub_scores_json": None,
        "fit_analysis": None,
    }
    base.update(cols)
    keys = ["dedup_key", "title", "company", "first_seen", "last_seen", *base.keys()]
    vals = [dedup_key, title, company, first_seen, first_seen, *base.values()]
    placeholders = ",".join("?" * len(keys))
    conn.execute(f"INSERT INTO jobs ({','.join(keys)}) VALUES ({placeholders})", vals)
    conn.commit()


def _version(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'dedup_normalizer_version'"
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# m100 schema_meta + seed
# ---------------------------------------------------------------------------


class TestMigration100Seed:
    def test_schema_meta_table_created(self, migrated_db):
        _, conn = migrated_db
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schema_meta)").fetchall()}
        assert cols == {"key", "value"}

    def test_fresh_db_seeds_at_current_version(self, migrated_db):
        """A fresh DB never ran the legacy dedup, but run_migrations re-keys it
        (empty -> no-op) and stamps the watermark to the current version."""
        _, conn = migrated_db
        assert _version(conn) == str(NORMALIZER_VERSION)

    def test_seed_value_one_when_legacy_sentinel_present(self):
        """When the migration_complete sentinel exists at m100 time, seed '1'."""
        from job_finder.web.migrations.m100_schema_meta_and_rekey import _seed_version
        from job_finder.web.migrations.types import MigrationContext

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE merge_log (id INTEGER PRIMARY KEY, canonical_key TEXT, "
            "merged_key TEXT, merge_source TEXT, merged_at TEXT)"
        )
        conn.execute(
            "INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at) "
            "VALUES ('__sentinel__', '__sentinel__', 'migration_complete', 'x')"
        )
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        ctx = MigrationContext(conn=conn, db_path=":memory:", user_data_root=".")

        _seed_version(ctx)

        assert (
            conn.execute(
                "SELECT value FROM schema_meta WHERE key='dedup_normalizer_version'"
            ).fetchone()[0]
            == "1"
        )
        conn.close()

    def test_seed_value_zero_when_no_sentinel(self):
        from job_finder.web.migrations.m100_schema_meta_and_rekey import _seed_version
        from job_finder.web.migrations.types import MigrationContext

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE merge_log (id INTEGER PRIMARY KEY, canonical_key TEXT, "
            "merged_key TEXT, merge_source TEXT, merged_at TEXT)"
        )
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        ctx = MigrationContext(conn=conn, db_path=":memory:", user_data_root=".")

        _seed_version(ctx)

        assert (
            conn.execute(
                "SELECT value FROM schema_meta WHERE key='dedup_normalizer_version'"
            ).fetchone()[0]
            == "0"
        )
        conn.close()

    def test_seed_idempotent_does_not_clobber(self):
        """A second seed call leaves an already-advanced watermark alone."""
        from job_finder.web.migrations.m100_schema_meta_and_rekey import _seed_version
        from job_finder.web.migrations.types import MigrationContext

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE merge_log (id INTEGER PRIMARY KEY, canonical_key TEXT, "
            "merged_key TEXT, merge_source TEXT, merged_at TEXT)"
        )
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('dedup_normalizer_version', '5')"
        )
        conn.commit()
        ctx = MigrationContext(conn=conn, db_path=":memory:", user_data_root=".")

        _seed_version(ctx)

        assert (
            conn.execute(
                "SELECT value FROM schema_meta WHERE key='dedup_normalizer_version'"
            ).fetchone()[0]
            == "5"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Standing re-key hook: _run_rekey_if_stale
# ---------------------------------------------------------------------------


class TestRekeyHook:
    def test_noop_when_version_matches(self, migrated_db):
        """Watermark already current -> hook merges nothing."""
        _, conn = migrated_db
        # Fresh DB is already stamped at current version by run_migrations.
        before = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]
        _post_hooks._run_rekey_if_stale(conn)
        after = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]
        assert before == after

    def test_defers_when_schema_meta_absent(self):
        """No schema_meta -> hook defers (no exception, no merge)."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY)")
        conn.commit()
        # Must not raise.
        _post_hooks._run_rekey_if_stale(conn)
        conn.close()

    def test_stale_version_triggers_merge_and_stamps(self, migrated_db):
        """Stored version < current -> merge stale-key dupes, stamp watermark."""
        path, conn = migrated_db
        from job_finder.models import Job

        # Two rows that are the SAME job under the current normalizer but carry
        # divergent stale keys (the #238 stranding scenario).
        _insert_job(
            conn,
            "capital one|84data scientist jobs",
            "84Data Scientist Jobs",
            "Capital One",
            "2026-01-01T00:00:00",
            pipeline_status="applied",
        )
        _insert_job(
            conn,
            "capital one|84 data scientist jobs",
            "84 Data Scientist Jobs",
            "Capital One",
            "2026-01-02T00:00:00",
        )
        # A genuinely distinct job that must survive untouched.
        _insert_job(conn, "acme|product manager", "Product Manager", "Acme", "2026-01-03T00:00:00")

        # Force the watermark stale.
        conn.execute("UPDATE schema_meta SET value = '1' WHERE key = 'dedup_normalizer_version'")
        conn.commit()

        _post_hooks._run_rekey_if_stale(conn)

        # Duplicate merged into one canonical; distinct job preserved.
        keys = {r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM jobs")}
        expected_dupe = Job.normalized_dedup_key("Capital One", "84 Data Scientist Jobs")
        expected_distinct = Job.normalized_dedup_key("Acme", "Product Manager")
        assert expected_dupe in keys
        assert expected_distinct in keys
        assert len(keys) == 2

        # Higher pipeline_status preserved on the merged canonical.
        status = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (expected_dupe,)
        ).fetchone()[0]
        assert status == "applied"

        # Watermark advanced to current.
        assert _version(conn) == str(NORMALIZER_VERSION)

        # Merge logged under the rekey_v{N} source.
        rk = conn.execute(
            "SELECT COUNT(*) FROM merge_log WHERE merge_source = ?",
            (f"rekey_v{NORMALIZER_VERSION}",),
        ).fetchone()[0]
        assert rk == 1

    def test_second_run_after_rekey_is_noop(self, migrated_db):
        """Once stamped current, a re-invocation does nothing."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "capital one|84data scientist jobs",
            "84Data Scientist Jobs",
            "Capital One",
            "2026-01-01T00:00:00",
        )
        _insert_job(
            conn,
            "capital one|84 data scientist jobs",
            "84 Data Scientist Jobs",
            "Capital One",
            "2026-01-02T00:00:00",
        )
        conn.execute("UPDATE schema_meta SET value = '1' WHERE key = 'dedup_normalizer_version'")
        conn.commit()

        _post_hooks._run_rekey_if_stale(conn)
        merges_first = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]

        _post_hooks._run_rekey_if_stale(conn)
        merges_second = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]

        assert merges_first == merges_second  # no new merges

    def test_version_bump_rearms_hook(self, migrated_db, monkeypatch):
        """Bumping NORMALIZER_VERSION (seen by the hook) re-arms re-keying.

        Even with no stale keys to merge, a bump must advance the watermark to
        the new value -- proving the version gate, not a one-time sentinel,
        governs the run.
        """
        path, conn = migrated_db
        assert _version(conn) == str(NORMALIZER_VERSION)

        bumped = NORMALIZER_VERSION + 1
        monkeypatch.setattr(_post_hooks, "NORMALIZER_VERSION", bumped)

        _post_hooks._run_rekey_if_stale(conn)

        assert _version(conn) == str(bumped)

    def test_merged_canonical_queued_for_rescore(self, migrated_db):
        """Merged canonicals get classification/sub_scores/fit_analysis NULLed."""
        path, conn = migrated_db
        from job_finder.models import Job

        _insert_job(
            conn,
            "capital one|84data scientist jobs",
            "84Data Scientist Jobs",
            "Capital One",
            "2026-01-01T00:00:00",
            classification="apply",
            sub_scores_json='{"comp_fit": 4}',
            fit_analysis="looked good",
        )
        _insert_job(
            conn,
            "capital one|84 data scientist jobs",
            "84 Data Scientist Jobs",
            "Capital One",
            "2026-01-02T00:00:00",
            classification="consider",
            sub_scores_json='{"comp_fit": 3}',
            fit_analysis="meh",
        )
        conn.execute("UPDATE schema_meta SET value = '1' WHERE key = 'dedup_normalizer_version'")
        conn.commit()

        _post_hooks._run_rekey_if_stale(conn)

        canonical = Job.normalized_dedup_key("Capital One", "84 Data Scientist Jobs")
        row = conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis FROM jobs WHERE dedup_key = ?",
            (canonical,),
        ).fetchone()
        assert row["classification"] is None
        assert row["sub_scores_json"] is None
        assert row["fit_analysis"] is None
