"""Migration 97 unit tests — heal residual HTML-polluted jd_full rows (post-F2).

Mirrors test_migration_079: m097 re-runs the same heal but routes through the
F2 boundary cleaner (``normalize_jd``) instead of calling ``html_to_plain_text``
directly, so healed rows are byte-identical to the live write path.
"""

import sqlite3

from job_finder.web.migrations.m097_heal_residual_html_jd_full import MIGRATION
from job_finder.web.migrations.types import MigrationContext
from tests.helpers.contract_triggers import (
    run_migrations_without_contract as run_migrations,
)


def _ctx(db_path):
    conn = sqlite3.connect(db_path)
    return conn, MigrationContext(conn=conn, db_path=db_path, user_data_root="", initial_version=0)


def _insert_job(conn, dedup_key, jd_full):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
        "first_seen, last_seen, jd_full) "
        "VALUES (?, 'DS', 'Co', 'Remote', '[]', '[]', "
        "'2026-06-03T00:00:00', '2026-06-03T00:00:00', ?)",
        (dedup_key, jd_full),
    )
    conn.commit()


def test_migration_version_is_97():
    assert MIGRATION.version == 97
    assert MIGRATION.py is not None


def test_heals_entity_escaped_html(tmp_db_path):
    """Escaped-HTML jd_full (Greenhouse-style residue) is converted to clean text."""
    run_migrations(tmp_db_path)
    conn, ctx = _ctx(tmp_db_path)
    try:
        _insert_job(
            conn,
            "gh-1",
            "&lt;p&gt;Build ML systems.&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;"
            "&lt;ul&gt;&lt;li&gt;Five years experience.&lt;/li&gt;&lt;/ul&gt;",
        )
        MIGRATION.py(ctx)
        jd = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'gh-1'").fetchone()[0]
    finally:
        conn.close()

    assert "&lt;" not in jd and "<p>" not in jd
    assert "Build ML systems" in jd
    assert "Requirements" in jd
    assert "Five years experience" in jd


def test_heals_raw_html(tmp_db_path):
    """Raw (unescaped) HTML tags are stripped too."""
    run_migrations(tmp_db_path)
    conn, ctx = _ctx(tmp_db_path)
    try:
        _insert_job(conn, "raw-1", "<p>Lead the platform team.</p><div>Own reliability.</div>")
        MIGRATION.py(ctx)
        jd = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'raw-1'").fetchone()[0]
    finally:
        conn.close()

    assert "<p>" not in jd and "<div>" not in jd
    assert "Lead the platform team" in jd
    assert "Own reliability" in jd


def test_leaves_plain_text_untouched(tmp_db_path):
    """A clean plain-text JD (even with a stray '<') is not matched/rewritten."""
    run_migrations(tmp_db_path)
    conn, ctx = _ctx(tmp_db_path)
    plain = "Senior Data Scientist. Comp under < 200k. Build models and ship them."
    try:
        _insert_job(conn, "plain-1", plain)
        MIGRATION.py(ctx)
        jd = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'plain-1'").fetchone()[0]
    finally:
        conn.close()

    assert jd == plain


def test_is_idempotent(tmp_db_path):
    """Second run is a no-op — healed rows no longer match the HTML filter."""
    run_migrations(tmp_db_path)
    conn, ctx = _ctx(tmp_db_path)
    try:
        _insert_job(
            conn, "idem-1", "&lt;p&gt;Build things end to end on the platform team.&lt;/p&gt;"
        )
        MIGRATION.py(ctx)
        first = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'idem-1'").fetchone()[0]
        MIGRATION.py(ctx)
        second = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'idem-1'").fetchone()[0]
    finally:
        conn.close()

    assert first == second
    assert "&lt;" not in first


def test_never_blanks_a_row(tmp_db_path):
    """jd_full is never set to empty even for tag-heavy input with real content."""
    run_migrations(tmp_db_path)
    conn, ctx = _ctx(tmp_db_path)
    try:
        _insert_job(conn, "nb-1", "<p>Actual job description content here for the heal pass.</p>")
        MIGRATION.py(ctx)
        jd = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'nb-1'").fetchone()[0]
    finally:
        conn.close()

    assert jd
    assert "Actual job description content here" in jd


def test_no_op_on_empty_db(tmp_db_path):
    """Running before the jobs table exists is a safe no-op (fresh install)."""
    conn = sqlite3.connect(tmp_db_path)
    ctx = MigrationContext(conn=conn, db_path=tmp_db_path, user_data_root="", initial_version=0)
    try:
        # No schema applied — jobs table absent.
        MIGRATION.py(ctx)  # must not raise
    finally:
        conn.close()
