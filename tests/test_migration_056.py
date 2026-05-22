"""Migration 56 unit tests — clear default-leaked scoring_provider='anthropic' tags.

Migration 20 added `scoring_provider TEXT DEFAULT 'anthropic'` to the jobs
table back when anthropic was the only scorer. The current multi-provider
cascade means every fresh INSERT inherits the DEFAULT even though no
anthropic call has run. Migration 56 clears those leaked tags on rows
that demonstrably never reached the scoring writer (scoring_model IS NULL
is the discriminator — the legitimate write path sets provider + model
atomically via COALESCE).
"""

import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m056_clear_anthropic_default_leak import MIGRATION


def test_migration_version_is_56():
    """MIGRATION.version must be 56 to satisfy the per-version filename contract."""
    assert MIGRATION.version == 56


def test_migration_description_present():
    """MIGRATION.description is required by db_migrate logging."""
    assert MIGRATION.description
    assert "scoring_provider" in MIGRATION.description.lower()


def test_migration_clears_default_leaked_tag(tmp_db_path):
    """A row with scoring_provider='anthropic' AND scoring_model IS NULL must be cleared."""
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, scoring_provider, scoring_model) "
            "VALUES (?, ?, ?, ?, ?, ?, '2026-05-22T00:00:00', '2026-05-22T00:00:00', ?, ?)",
            ("leaked-1", "DS", "Co", "Remote", "[]", "[]", "anthropic", None),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run all migrations is a no-op on already-applied; apply just m056 by SQL.
    conn = sqlite3.connect(tmp_db_path)
    try:
        for stmt in MIGRATION.sql:
            conn.execute(stmt)
        conn.commit()
        row = conn.execute(
            "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = 'leaked-1'"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] is None, f"Expected scoring_provider cleared to NULL, got {row[0]!r}"
    assert row[1] is None


def test_migration_preserves_legitimate_attribution(tmp_db_path):
    """A row with scoring_provider='anthropic' AND scoring_model set must NOT be cleared.

    The discriminator is scoring_model IS NULL because the legitimate
    writer (persist_job_assessment) sets provider and model together via
    COALESCE. Any row that has a model is a real attribution we must
    preserve.
    """
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, scoring_provider, scoring_model) "
            "VALUES (?, ?, ?, ?, ?, ?, '2026-05-22T00:00:00', '2026-05-22T00:00:00', ?, ?)",
            ("real-anthropic", "DS", "Co", "Remote", "[]", "[]", "anthropic", "claude-3-opus"),
        )
        conn.commit()

        for stmt in MIGRATION.sql:
            conn.execute(stmt)
        conn.commit()

        row = conn.execute(
            "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = 'real-anthropic'"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "anthropic"
    assert row[1] == "claude-3-opus"


def test_migration_does_not_touch_other_providers(tmp_db_path):
    """Rows scored by ollama / groq / gemini / etc. must be untouched even if scoring_model is somehow NULL.

    Defense-in-depth: only the 'anthropic' literal triggers the heal pass.
    """
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, scoring_provider, scoring_model) "
            "VALUES (?, ?, ?, ?, ?, ?, '2026-05-22T00:00:00', '2026-05-22T00:00:00', ?, ?)",
            ("ollama-job", "DS", "Co", "Remote", "[]", "[]", "ollama", None),
        )
        conn.commit()

        for stmt in MIGRATION.sql:
            conn.execute(stmt)
        conn.commit()

        row = conn.execute(
            "SELECT scoring_provider FROM jobs WHERE dedup_key = 'ollama-job'"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "ollama"


def test_migration_is_idempotent_on_rerun(tmp_db_path):
    """Running run_migrations twice on the same DB does not raise — m056 is a pure UPDATE."""
    run_migrations(tmp_db_path)
    run_migrations(tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 56


def test_upsert_job_post_migration_does_not_reintroduce_leak(tmp_db_path):
    """Integration: after migrations run, a fresh upsert_job INSERT must not produce a leaked tag.

    Migration 56 cleans up historical leaks. The defense-in-depth INSERT
    fix in upsert_job (Stage 7.7) prevents new ones from accruing. This
    test pins both pieces of the fix together: migration ran, INSERT ran,
    no 'anthropic' leak appears in the row.
    """
    from job_finder.db import upsert_job
    from job_finder.models import Job

    run_migrations(tmp_db_path)

    job = Job(
        title="Senior Data Scientist",
        company="PostMigrationCo",
        location="Remote",
        source="linkedin",
        source_url="https://linkedin.com/jobs/view/999/",
        source_id="999",
    )

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        upsert_job(conn, job)
        row = conn.execute(
            "SELECT scoring_provider FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
    finally:
        conn.close()

    assert row["scoring_provider"] is None
