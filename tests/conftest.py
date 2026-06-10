"""Shared test fixtures for job-finder test suite."""

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

# Migration 41 (Plan 5) has a backup-recency preflight that raises
# MigrationBlockedError unless a recent backup_userdata_*.tar.gz exists or
# GSD_BACKUP_CONFIRMED=1 is set. The test suite creates temp DBs and runs the
# full migration chain on every fixture instantiation, so we acknowledge the
# override session-wide. Individual tests that need to exercise the gate set
# up their own os.environ patches.
os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")

# pystray's Xorg backend connects to the X display at IMPORT time, so a bare
# `import pystray` on a headless Linux CI runner ($DISPLAY unset) raises
# Xlib.error.DisplayNameError and crashes collection of tests/test_tray.py.
# The `dummy` backend is pystray's documented display-free backend; it still
# exposes Icon / Menu / MenuItem (which the tray tests mock anyway). Set it
# session-wide before any test module imports pystray. Harmless on Windows/macOS.
os.environ.setdefault("PYSTRAY_BACKEND", "dummy")


@pytest.fixture(autouse=True)
def _isolated_user_data_root(tmp_path, monkeypatch):
    """Point JOB_CANNON_USER_DATA_DIR at a per-test temp dir for EVERY test.

    Production code paths write into the user-data root as side effects
    (heal contribution bundles on adoption, OAuth artifacts, update-check
    cache); without this, any test exercising those paths litters the real
    OS user-data directory — and on a dev machine the suite silently READS
    the developer's real config.yaml.

    CI's contract (ci.yml "seed config" step) is that a config.yaml built
    from config.example.yaml exists in the user-data root, and many tests
    rely on a fail-fast load_config() succeeding ambiently — so this fixture
    seeds the same file. Tests that need different env semantics override
    with their own monkeypatch.setenv/delenv (test-level monkeypatch wins).
    """
    import shutil
    from pathlib import Path

    root = tmp_path / "_userdata"
    root.mkdir(parents=True, exist_ok=True)
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    shutil.copyfile(example, root / "config.yaml")
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(root))


def _seed_onboarding_complete(db_path: str) -> None:
    """Seed onboarding_state(id=1, onboarding_complete=1) so the @before_request gate does not redirect tests to /onboarding/welcome.

    Called by every fixture that returns a Flask app from create_app(). Test files that
    need the gate to redirect (e.g., test_onboarding_gate.py) use the app_unconfigured
    fixture below, which UPDATEs the row back to 0.

    Note: wizard_data column is added in Migration 54 (plan 42-02); this helper only
    seeds columns that exist in Migration 53.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete) VALUES (1, 1)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def tmp_db_path():
    """Create a temporary SQLite database file, yield path, clean up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def sample_db_with_jobs():
    """Create a temp DB with the OLD schema (matching db.py._init_tables).

    Inserts 3 sample job rows with realistic data. Simulates the existing
    jobs.db before migration.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    # Create the old schema exactly as in job_finder/db.py
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            sources TEXT DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            source_id TEXT DEFAULT '',
            salary_min INTEGER,
            salary_max INTEGER,
            description TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            score REAL DEFAULT 0,
            score_breakdown TEXT DEFAULT '{}',
            user_interest TEXT DEFAULT 'unreviewed'
        );

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            jobs_fetched INTEGER DEFAULT 0,
            jobs_new INTEGER DEFAULT 0,
            jobs_scored INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_interest ON jobs(user_interest);
        CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen DESC);
        """
    )

    # Insert 3 sample job rows with realistic data
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, user_interest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "thumbtack|senior data scientist|united states",
                "Senior Data Scientist",
                "Thumbtack",
                "United States",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/4364166509/"]',
                "4364166509",
                180000,
                240000,
                "Build data products at Thumbtack.",
                "2026-03-01T10:00:00",
                "2026-03-09T10:00:00",
                8.5,
                '{"skills": 0.9, "title": 0.85}',
                "reviewing",
            ),
            (
                "betterhelp|data scientist experimentation|san jose ca",
                "Data Scientist, Experimentation",
                "BetterHelp",
                "San Jose, CA",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/4248973844/"]',
                "4248973844",
                150000,
                200000,
                "Run A/B tests at scale.",
                "2026-03-02T11:00:00",
                "2026-03-09T11:00:00",
                7.2,
                '{"skills": 0.75, "title": 0.7}',
                "unreviewed",
            ),
            (
                "toast|staff data scientist|united states",
                "Staff Data Scientist",
                "Toast",
                "United States",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/4337163287/"]',
                "4337163287",
                200000,
                280000,
                "Lead data science for restaurant tech platform.",
                "2026-03-03T12:00:00",
                "2026-03-09T12:00:00",
                9.1,
                '{"skills": 0.95, "title": 0.9}',
                "interested",
            ),
        ],
    )
    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def app(tmp_db_path):
    """Standard test Flask app with full config superset.

    Includes all config keys needed by any test file to avoid KeyErrors
    in blueprints. Individual test files that need custom DB setup
    (e.g., test_pipeline.py with pre-inserted jobs) should define
    their own local app fixture.
    """
    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {
            "min_score_threshold": 40,
            "daily_budget_usd": 25.0,
        },
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    _seed_onboarding_complete(tmp_db_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """Flask test client from the shared app fixture."""
    return app.test_client()


@pytest.fixture
def app_unconfigured(tmp_db_path):
    """Flask app with onboarding_complete=0 so @before_request gate redirects to /onboarding/welcome.

    Used by tests/test_onboarding_gate.py to verify the redirect lifecycle. Mirrors the
    standard `app` fixture but UPDATEs onboarding_state back to 0 after seeding (which
    run_migrations may have already triggered an INSERT for).
    """
    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)

    import sqlite3

    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete) VALUES (1, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    application.config["TESTING"] = True
    return application


@pytest.fixture(scope="session")
def _migrated_template_db(tmp_path_factory):
    """Build the fully-migrated schema ONCE per test session.

    Per-test fixtures get private copies via the sqlite3 backup API (~1-5ms)
    instead of re-running all migrations (~141ms each ≈ ~50s of pure setup
    across the ~388 tests that use migrated_db*). Isolation is identical: every
    test still operates on its own private file. The template is read-only —
    never write to the path this returns. The backup API (not a raw file copy)
    is used so the clone is correct regardless of the template's WAL/journal
    state — no -wal/-shm sidecar handling needed.
    """
    from job_finder.web.db_migrate import run_migrations

    path = tmp_path_factory.mktemp("template") / "migrated_template.db"
    run_migrations(str(path))
    return str(path)


def _clone_template(template_path: str) -> str:
    """Create a private on-disk copy of the migrated template; return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(template_path)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path


@pytest.fixture
def migrated_db(_migrated_template_db):
    """Create a temp DB, run ALL migrations (including Migration 2), yield (path, conn).

    This is the standard fixture for all Phase 2 AI scoring tests. The schema is
    built once per session (_migrated_template_db) and cloned per-test; closes
    connection and removes file on teardown.
    """
    path = _clone_template(_migrated_template_db)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield path, conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture(scope="class")
def migrated_db_class():
    """Shared migrated DB for test classes that don't depend on clean initial state.

    Each test in the class shares the same DB. Only safe for classes where ALL
    tests are either pure schema reads (PRAGMA queries, sqlite_master reads) or
    insert rows with unique keys and never assert on initial row counts.

    Safe candidates confirmed by audit (Plan 20-01):
    - TestMigration13: pure PRAGMA reads (schema verification only)
    - TestMigration2: pure PRAGMA reads (schema verification only)
    - TestMigration3: schema checks + unique constraint test (no cross-test row count assertions)

    NOT safe (cross-test state pollution via row counts):
    - TestDbHelpers: tests assert len(pending_detections)==1 but accumulate rows across tests
    """
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield path, conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def migrated_db_with_jobs(_migrated_template_db):
    """Create a temp DB, run ALL migrations (including Migration 3), insert 3 sample jobs.

    Extends migrated_db with pre-inserted jobs that have pipeline_status so
    pipeline_detector integration tests have realistic data to work with.
    Yields (path, conn). Closes and removes file on teardown.
    """
    path = _clone_template(_migrated_template_db)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    from datetime import datetime, timedelta

    now = datetime.now().isoformat()
    five_days_ago = (datetime.now() - timedelta(days=5)).isoformat()

    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, user_interest,
             pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "stripe|senior data scientist|remote",
                "Senior Data Scientist",
                "Stripe",
                "Remote",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/1111/"]',
                "1111",
                180000,
                240000,
                "Build data products at Stripe.",
                five_days_ago,
                now,
                8.5,
                "{}",
                "interested",
                "reviewing",
            ),
            (
                "betterhelp|data scientist|san jose ca",
                "Data Scientist",
                "BetterHelp",
                "San Jose, CA",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/2222/"]',
                "2222",
                150000,
                200000,
                "Run experiments at BetterHelp.",
                five_days_ago,
                now,
                7.2,
                "{}",
                "unreviewed",
                "reviewing",
            ),
            (
                "thumbtack|staff data scientist|united states",
                "Staff Data Scientist",
                "Thumbtack",
                "United States",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/3333/"]',
                "3333",
                200000,
                280000,
                "Lead data science at Thumbtack.",
                five_days_ago,
                now,
                9.1,
                "{}",
                "interested",
                "applied",
            ),
        ],
    )
    conn.commit()

    yield path, conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture(autouse=True)
def mock_run_oneshot():
    """Auto-mock _run_oneshot so no test accidentally invokes the real Claude CLI.

    Returns a superset envelope that works for both legacy (low/mid) and
    v3.0 (JobAssessment) call sites. structured_output carries the legacy
    {score, summary} shape (keeps pre-Phase-34 tests green) while the result
    JSON carries the v3 ordinal fields at the top level (matches
    JOB_ASSESSMENT_SCHEMA) so dispatcher calls through call_model(tier='scoring')
    also parse cleanly. Individual test classes override at the module-import
    level for more specific behavior.
    """
    v3_payload = {
        # v3.0 top-level ordinal sub-scores (CONTEXT D-05).
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
        "rationale": {
            "strengths": ["stub strength"],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        },
        "legitimacy_note": "",
    }
    legacy_payload = {"score": 75, "summary": "Good match"}
    # Merge: legacy keys available as top-level alongside v3 keys. No key
    # collision because the legacy schema never emitted title_fit etc.
    merged = {**legacy_payload, **v3_payload}
    envelope = {
        "is_error": False,
        "result": json.dumps(merged),
        "structured_output": merged,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "total_cost_usd": 0.001,
    }
    with patch("job_finder.web.claude_client._run_oneshot", return_value=envelope) as mock:
        yield mock


@pytest.fixture
def mock_run_oneshot_legacy():
    """Opt-in fixture for tests exercising the pre-v3 legacy path.

    Returns the low/mid-tier envelope only, without the v3 ordinal
    fields. Overrides the autouse mock_run_oneshot when declared explicitly
    in a test function's signature. Removed in Plan 4 alongside the
    low_tier_scorer.py / mid_tier_evaluator.py deletion.
    """
    envelope = {
        "is_error": False,
        "result": json.dumps({"score": 75, "summary": "Good match"}),
        "structured_output": {"score": 75, "summary": "Good match"},
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "total_cost_usd": 0.001,
    }
    with patch("job_finder.web.claude_client._run_oneshot", return_value=envelope) as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_liveness_check():
    """Auto-mock check_job_liveness so no test accidentally issues real HTTP
    probes during mid-tier's liveness gate.

    Defaults to INCONCLUSIVE — the safe pass-through that neither archives
    the job nor blocks evaluation. Tests that specifically exercise the gate
    override this with a nested ``with patch.object(sr, "check_job_liveness", ...)``
    which takes precedence until the inner context exits.
    """
    with patch(
        "job_finder.web.scoring_runner.check_job_liveness",
        return_value="inconclusive",
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def block_claude_cli_subprocess():
    """Prevent any test from spawning the real `claude -p` CLI subprocess.

    job_finder.web.claude_enricher.enrich_companies_via_claude() shells out to
    `claude -p` (subprocess.run, timeout=120) for "Tier 3" homepage discovery.
    Its ONLY production caller is homepage_discoverer._try_claude_enricher()
    (lazily imported at homepage_discoverer.py:378), which discover_homepage()
    and run_homepage_discovery() call UNCONDITIONALLY. Without this fixture,
    tests that reach Tier 3 invoke the real CLI: 12-63s each, and pass/fail
    depends on what the live CLI returns (a documented flake — see
    test_homepage_discoverer.py:158). The _run_oneshot mock does NOT cover this
    path (separate subprocess, not the claude_client._run_oneshot envelope).

    We block at the FUNCTION boundary (return []) — NOT by patching
    claude_enricher.subprocess.run. claude_enricher does ``import subprocess``,
    so claude_enricher.subprocess IS the shared subprocess module singleton;
    patching claude_enricher.subprocess.run would replace subprocess.run
    GLOBALLY and break every test that legitimately spawns a process (the CLI
    smoke tests, scheduler pidfile self-release, etc.). [] is exactly what the
    real path returns when the CLI is absent (its own ``except FileNotFoundError``
    handler returns []), so callers see identical behavior. No test validates the
    enricher's real output, so this is safe suite-wide. Patched at the source
    module so the lazy import in _try_claude_enricher is covered; a future test
    of claude_enricher internals can override it with its own patch.
    """
    with patch(
        "job_finder.web.claude_enricher.enrich_companies_via_claude",
        return_value=[],
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_ensure_ollama_running():
    """Auto-mock the scheduler's Ollama probe so tests don't touch a live service.

    ``init_scheduler()`` calls ``probe_ollama()``, which makes HTTP requests to
    a local Ollama service and may spawn ``ollama serve``. Tests exercising the
    scheduler-start path don't want real network calls, so patch probe_ollama to
    return ``Unavailable()`` suite-wide (no service, no spawn, no orphan procs).
    Mirrors the ``_run_oneshot`` / ``mock_liveness_check`` /
    ``block_claude_cli_subprocess`` autouse mocks.
    """
    from job_finder.web.scheduler._ollama import Unavailable

    with patch(
        "job_finder.web.scheduler.probe_ollama",
        return_value=Unavailable(),
    ) as mock:
        yield mock


@pytest.fixture
def cascade_config_low():
    """Config with Ollama primary + Anthropic CLI fallback for the low tier.

    Mirrors backfill_enrichment._OFFLINE_PROVIDERS so call_model() takes the
    cascade branch (non-empty fallback_chain) and raises
    ProviderCascadeExhaustedError — not generic RuntimeError — when every
    provider fails.
    """
    return {
        "providers": {
            "low": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "anthropic", "model": "claude-haiku-4-5"},
                ],
            },
        },
    }


@pytest.fixture
def cascade_config_mid():
    """Config with Ollama primary + Anthropic CLI fallback for the mid tier."""
    return {
        "providers": {
            "mid": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                ],
            },
        },
    }


@pytest.fixture
def cascade_config_scoring():
    """Phase 34 Plan 2 — config with the v3.0 unified scoring tier.

    Mirrors providers.scoring in the live config.yaml. qwen2.5:14b is the
    Phase 33 shootout winner (CONTEXT D-01). The fallback chain inherits
    the full cascade per D-10. Tests that exercise the unified path should
    declare this fixture AND set use_unified_scorer: True when constructing
    their full config dict.
    """
    return {
        "providers": {
            "scoring": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                    {"provider": "cerebras", "model": "llama3.3-70b"},
                    {"provider": "gemini", "model": "gemini-2.0-flash"},
                    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                ],
            },
        },
        "use_unified_scorer": True,
    }


@pytest.fixture
def make_model_result():
    """Factory for ModelResult instances used by cascade-dispatch tests.

    Returns a callable that accepts ``data`` and optional provider/cost/token
    overrides. Keeps the defaults Ollama-shaped so a test using only
    ``make_model_result({"score": 80})`` reads as "cascade primary succeeded".
    """
    from job_finder.web.model_provider import ModelResult

    def _factory(
        data,
        *,
        provider="ollama",
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="qwen2.5:14b",
        schema_valid=True,
    ):
        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider=provider,
            schema_valid=schema_valid,
        )

    return _factory


@pytest.fixture(autouse=True)
def mock_scheduler_pidfile():
    """Auto-mock _acquire_scheduler_pidfile so tests do not collide with a
    real run.py Flask instance that may be holding the pidfile.

    The production pidfile prevents two live Python processes from both
    running the 0,8,16 cron schedule. In tests we always want init_scheduler
    to proceed as if it owns the lock — hermetic isolation from whatever
    pidfile happens to exist on disk at test time.
    """
    with patch(
        "job_finder.web.scheduler._acquire_scheduler_pidfile",
        return_value=True,
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Keyring isolation (Item 3 commit 3.3 — KEYRING-v5.1)
# ---------------------------------------------------------------------------
# This MUST be autouse=True so every test runs against an in-memory backend.
# Without it, tests that exercise set_secret() would write to the developer's
# real OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret
# Service) and leave dangling entries behind. The fixture also resets the
# module-level deprecation-warning memo and the _KEYRING_UNAVAILABLE flag in
# job_finder.secrets so test order can't affect outcomes.


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    """Install an in-memory keyring backend for every test."""
    from tests.helpers.keyring_helpers import InMemoryKeyring

    backend = InMemoryKeyring()
    monkeypatch.setattr("keyring.core._keyring_backend", backend)

    from job_finder import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_KEYRING_UNAVAILABLE", False)
    secrets_mod._warned.clear()
    yield backend
    secrets_mod._warned.clear()


# ---------------------------------------------------------------------------
# Collection-count sentinel (Reconciliation Plan v1 R2.3)
# ---------------------------------------------------------------------------
# Records the number of items pytest collected so test_collection_invariants
# can assert the suite hasn't silently dropped tests (e.g., a skipif
# evaluating True when it shouldn't, a fixture-error swallowing a module,
# a broken import that pytest tolerates with --collect-ignore-glob).
#
# This is a defensive sentinel against the F-C1/C1.5/C1.6/C1.7/C2 family of
# silent-skip findings recurring. The floor is calibrated below the current
# count with margin, and is updated deliberately when adding/removing tests.


def pytest_collection_modifyitems(config, items):
    """Stash the collected count on the config so the sentinel can read it."""
    config._collected_count = len(items)
