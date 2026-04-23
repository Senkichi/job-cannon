"""Shared test fixtures for job-finder test suite."""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Migration 41 (Plan 5) has a backup-recency preflight that raises
# MigrationBlockedError unless a recent backup_userdata_*.tar.gz exists or
# GSD_BACKUP_CONFIRMED=1 is set. The test suite creates temp DBs and runs the
# full migration chain on every fixture instantiation, so we acknowledge the
# override session-wide. Individual tests that need to exercise the gate set
# up their own os.environ patches.
os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")


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
    application.config["TESTING"] = True
    return application

@pytest.fixture
def client(app):
    """Flask test client from the shared app fixture."""
    return app.test_client()

@pytest.fixture
def migrated_db():
    """Create a temp DB, run ALL migrations (including Migration 2), yield (path, conn).

    This is the standard fixture for all Phase 2 AI scoring tests.
    Closes connection and removes file on teardown.
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
def migrated_db_with_jobs():
    """Create a temp DB, run ALL migrations (including Migration 3), insert 3 sample jobs.

    Extends migrated_db with pre-inserted jobs that have pipeline_status so
    pipeline_detector integration tests have realistic data to work with.
    Yields (path, conn). Closes and removes file on teardown.
    """
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

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
                180000, 240000,
                "Build data products at Stripe.",
                five_days_ago, now, 8.5, '{}', "interested", "reviewing",
            ),
            (
                "betterhelp|data scientist|san jose ca",
                "Data Scientist",
                "BetterHelp",
                "San Jose, CA",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/2222/"]',
                "2222",
                150000, 200000,
                "Run experiments at BetterHelp.",
                five_days_ago, now, 7.2, '{}', "unreviewed", "reviewing",
            ),
            (
                "thumbtack|staff data scientist|united states",
                "Staff Data Scientist",
                "Thumbtack",
                "United States",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/3333/"]',
                "3333",
                200000, 280000,
                "Lead data science at Thumbtack.",
                five_days_ago, now, 9.1, '{}', "interested", "applied",
            ),
        ],
    )
    conn.commit()

    yield path, conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)

@pytest.fixture
def sample_resume_data():
    """Return a structured resume dict for use across resume test classes.

    Contains name, contact_line, summary, skills, positions (2 entries with
    achievements), and education (1 entry). Reusable across all resume tests.
    """
    return {
        "name": "Jane Doe",
        "contact_line": "jane@example.com | (555) 555-1234 | linkedin.com/in/janedoe | San Francisco, CA",
        "summary": (
            "Data scientist with 8 years of experience building machine learning systems "
            "at scale. Specializes in experimentation platforms and causal inference."
        ),
        "skills": ["Python", "SQL", "Machine Learning", "A/B Testing", "Spark", "dbt"],
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "Acme Corp",
                "dates": "Jan 2021 - Present",
                "achievements": [
                    "Led A/B testing platform serving 10M daily active users",
                    "Reduced model inference latency by 40% via feature store refactoring",
                ],
            },
            {
                "title": "Data Scientist",
                "company": "Beta Inc",
                "dates": "Mar 2018 - Dec 2020",
                "achievements": [
                    "Built customer churn model with 87% AUC, saving $2M annually",
                    "Mentored 3 junior data scientists and established team best practices",
                ],
            },
        ],
        "education": [
            {
                "degree": "M.S. Statistics",
                "institution": "Stanford University",
                "year": "2018",
            }
        ],
    }

@pytest.fixture(autouse=True)
def mock_run_oneshot():
    """Auto-mock _run_oneshot so no test accidentally invokes the real Claude CLI.

    Returns a superset envelope that works for both legacy (Haiku/Sonnet) and
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

    Returns the Haiku/Sonnet-shaped envelope only, without the v3 ordinal
    fields. Overrides the autouse mock_run_oneshot when declared explicitly
    in a test function's signature. Removed in Plan 4 alongside the
    haiku_scorer.py / sonnet_evaluator.py deletion.
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
    probes during Sonnet's liveness gate.

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


@pytest.fixture
def cascade_config_haiku():
    """Config with Ollama primary + Anthropic CLI fallback for the haiku tier.

    Mirrors backfill_enrichment._OFFLINE_PROVIDERS so call_model() takes the
    cascade branch (non-empty fallback_chain) and raises
    ProviderCascadeExhaustedError — not generic RuntimeError — when every
    provider fails.
    """
    return {
        "providers": {
            "haiku": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "anthropic", "model": "claude-haiku-4-5"},
                ],
            },
        },
    }


@pytest.fixture
def cascade_config_sonnet():
    """Config with Ollama primary + Anthropic CLI fallback for the sonnet tier."""
    return {
        "providers": {
            "sonnet": {
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

    def _factory(data, *, provider="ollama", cost_usd=0.0,
                 input_tokens=100, output_tokens=50,
                 model="qwen2.5:14b", schema_valid=True):
        return ModelResult(
            data=data, cost_usd=cost_usd,
            input_tokens=input_tokens, output_tokens=output_tokens,
            model=model, provider=provider, schema_valid=schema_valid,
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

