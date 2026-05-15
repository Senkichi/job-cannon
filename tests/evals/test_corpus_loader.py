"""Unit tests for cascade audit CorpusLoader (Phase 36, AUDIT-06).

Verifies:
- All 6 callsites are sampled at Round 0.
- dedup_keys.json is persisted to artifacts/round_0/.
- jd_full / careers_nav_recipe contents are cached on disk for determinism.
- Round 1 reload uses persisted dedup_keys (reproducibility across rounds).
- Round 1 raises FileNotFoundError when dedup_keys.json is absent.
- Queries are parameterized — adversarial dedup_keys cannot drop or mutate
  the production tables.
- _safe_cache_stem strips path-illegal characters and produces stable stems.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evals.cascade_audit.corpus_loader import CorpusLoader, _safe_cache_stem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_in_memory_db() -> sqlite3.Connection:
    """Build an in-memory SQLite DB matching the production columns the
    CorpusLoader samplers touch.

    Production schema (per .planning/codebase/ARCHITECTURE.md and the columns
    referenced in corpus_loader.py):
      jobs(dedup_key TEXT PRIMARY KEY, jd_full TEXT, description TEXT, ...)
      companies(id INTEGER PRIMARY KEY, homepage_url TEXT, name TEXT,
                careers_nav_recipe TEXT, ...)

    Note: the companies sampler aliases CAST(id AS TEXT) AS dedup_key — fixtures
    must INSERT id values (not dedup_key) for that table.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            jd_full TEXT,
            description TEXT
        );
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY,
            homepage_url TEXT,
            name TEXT,
            careers_nav_recipe TEXT
        );
        """
    )

    # Seed jobs: enough rows with jd_full > 400 chars and description.
    long_jd = "Lorem ipsum dolor sit amet. " * 25  # ~700 chars
    job_rows = [
        (f"jobs|fixture|{i}", long_jd, f"Short description for job {i}.")
        for i in range(10)
    ]
    conn.executemany(
        "INSERT INTO jobs (dedup_key, jd_full, description) VALUES (?, ?, ?)",
        job_rows,
    )

    # Seed companies: must be >=50 rows so the extract_jobs sampler (hard-coded
    # 50) returns its full ask without exhausting the table.
    company_rows = [
        (
            i,
            f"https://example-{i}.com",
            f"Company {i}",
            json.dumps({"steps": [{"action": "click", "selector": f"#careers-{i}"}]}),
        )
        for i in range(1, 61)  # 60 companies
    ]
    conn.executemany(
        "INSERT INTO companies (id, homepage_url, name, careers_nav_recipe) "
        "VALUES (?, ?, ?, ?)",
        company_rows,
    )
    conn.commit()
    return conn


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    conn = _make_in_memory_db()
    yield conn
    conn.close()


@pytest.fixture
def loader(tmp_path: Path) -> CorpusLoader:
    """CorpusLoader pointed at a fresh per-test artifact dir.

    db_path is unused in tests — the samplers receive the connection directly,
    not the path string — so a placeholder is fine.
    """
    return CorpusLoader(artifact_dir=tmp_path / "artifacts", db_path=":memory:")


# ---------------------------------------------------------------------------
# Round 0 behavior
# ---------------------------------------------------------------------------


def test_load_round_0_samples_all_six_callsites(
    loader: CorpusLoader, db_conn: sqlite3.Connection
) -> None:
    """Round 0 must return a corpus dict keyed by all 6 non-scoring callsites."""
    corpus = loader.load_round_0(n_per_callsite=3, conn=db_conn)

    expected_callsites = {
        "parse_structured_fields",
        "find_careers_url",
        "extract_jobs",
        "description_reformat",
        "company_research",
        "ai_nav_discovery",
    }
    assert set(corpus.keys()) == expected_callsites

    # Each list non-empty (fixture seeds enough rows for every sampler).
    for callsite, rows in corpus.items():
        assert len(rows) > 0, f"callsite {callsite} returned 0 rows"
        # Every row carries dedup_key for downstream reproducibility.
        for row in rows:
            assert "dedup_key" in row, f"{callsite} row missing dedup_key"


def test_load_round_0_persists_dedup_keys(
    loader: CorpusLoader, db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """dedup_keys.json is written to round_0/ with one key array per callsite."""
    corpus = loader.load_round_0(n_per_callsite=3, conn=db_conn)

    keys_file = tmp_path / "artifacts" / "round_0" / "dedup_keys.json"
    assert keys_file.exists(), "dedup_keys.json was not persisted"

    persisted = json.loads(keys_file.read_text(encoding="utf-8"))

    expected_callsites = {
        "parse_structured_fields",
        "find_careers_url",
        "extract_jobs",
        "description_reformat",
        "company_research",
        "ai_nav_discovery",
    }
    assert set(persisted.keys()) == expected_callsites

    # Persisted arrays match what was returned.
    for callsite, rows in corpus.items():
        assert persisted[callsite] == [row["dedup_key"] for row in rows]


def test_load_round_0_caches_jd_text(
    loader: CorpusLoader, db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Round 0 caches jd_full content under round_0/jd/ for determinism."""
    corpus = loader.load_round_0(n_per_callsite=3, conn=db_conn)

    jd_dir = tmp_path / "artifacts" / "round_0" / "jd"
    assert jd_dir.is_dir(), "round_0/jd/ directory was not created"

    cached_files = list(jd_dir.glob("*.txt"))
    # One cache file per parse_structured_fields row.
    assert len(cached_files) == len(corpus["parse_structured_fields"])

    # Sampled jd_full contents round-trip onto disk.
    sampled_jds = {row["jd_full"] for row in corpus["parse_structured_fields"]}
    cached_contents = {p.read_text(encoding="utf-8") for p in cached_files}
    assert sampled_jds == cached_contents


def test_load_round_0_caches_recipe(
    loader: CorpusLoader, db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Round 0 caches careers_nav_recipe content under round_0/recipes/."""
    corpus = loader.load_round_0(n_per_callsite=3, conn=db_conn)

    recipe_dir = tmp_path / "artifacts" / "round_0" / "recipes"
    assert recipe_dir.is_dir(), "round_0/recipes/ was not created"

    cached_files = list(recipe_dir.glob("*.json"))
    assert len(cached_files) == len(corpus["ai_nav_discovery"])

    sampled_recipes = {row["careers_nav_recipe"] for row in corpus["ai_nav_discovery"]}
    cached_contents = {p.read_text(encoding="utf-8") for p in cached_files}
    assert sampled_recipes == cached_contents


# ---------------------------------------------------------------------------
# Round 1 reproducibility
# ---------------------------------------------------------------------------


def test_load_round_1_uses_persisted_dedup_keys(
    loader: CorpusLoader, db_conn: sqlite3.Connection
) -> None:
    """Reloading Round 1 must return the same dedup_keys as Round 0 — the
    contract that makes shadow-replay reproducible across providers/rounds.
    """
    round_0_corpus = loader.load_round_0(n_per_callsite=3, conn=db_conn)
    round_1_corpus = loader.load_round_1(conn=db_conn)

    assert set(round_0_corpus.keys()) == set(round_1_corpus.keys())

    for callsite in round_0_corpus:
        round_0_keys = sorted(row["dedup_key"] for row in round_0_corpus[callsite])
        round_1_keys = sorted(row["dedup_key"] for row in round_1_corpus[callsite])
        assert round_0_keys == round_1_keys, (
            f"{callsite}: round 1 keys diverged from round 0 — "
            f"reproducibility contract broken"
        )


def test_load_round_1_raises_when_round_0_missing(
    loader: CorpusLoader, db_conn: sqlite3.Connection
) -> None:
    """Round 1 must fail loudly (FileNotFoundError) if Round 0 wasn't run."""
    with pytest.raises(FileNotFoundError, match="dedup_keys.json"):
        loader.load_round_1(conn=db_conn)


# ---------------------------------------------------------------------------
# SQL injection resistance
# ---------------------------------------------------------------------------


def test_parameterized_queries_no_injection(
    loader: CorpusLoader, db_conn: sqlite3.Connection
) -> None:
    """Adversarial dedup_key values must NOT execute as SQL.

    AUDIT-06 mandates parameterized queries throughout corpus_loader. We pass
    a classic injection payload as a dedup_key and confirm:
      1. _load_by_keys does not raise sqlite OperationalError
      2. The jobs table still exists and still holds the seeded rows
      3. No row is returned for the bogus key (it doesn't match any real row)
    """
    # Seed pre-state we can verify post-call.
    pre_count = db_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert pre_count > 0, "fixture precondition: jobs table seeded"

    adversarial_keys = [
        "x'); DROP TABLE jobs;--",
        "' OR 1=1 --",
        "'; DELETE FROM companies; --",
    ]

    # Should not raise; should return an empty list (none match seeded keys).
    result = loader._load_by_keys("jobs", adversarial_keys, db_conn)
    assert result == []

    # Tables intact.
    tables = {
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "jobs" in tables, "jobs table dropped — SQL injection succeeded"
    assert "companies" in tables, "companies table dropped — SQL injection succeeded"

    # Row count unchanged.
    post_count = db_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert post_count == pre_count, "jobs row count changed — SQL injection succeeded"

    companies_count = db_conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    assert companies_count > 0, "companies were deleted — SQL injection succeeded"


# ---------------------------------------------------------------------------
# Cache-stem helper
# ---------------------------------------------------------------------------


def test_safe_cache_stem_handles_path_chars() -> None:
    """_safe_cache_stem must strip path-illegal chars and stay deterministic."""
    nasty = 'foo/bar\\baz:qux*key?<>"|\x00'
    stem = _safe_cache_stem(nasty)

    # Must not contain any of the illegal Windows path chars.
    forbidden = set('<>:"/\\|?*')
    for ch in forbidden:
        assert ch not in stem, f"stem leaked illegal char {ch!r}: {stem}"
    assert "\x00" not in stem

    # Same input → same stem (digest is deterministic).
    assert _safe_cache_stem(nasty) == stem

    # Different input → different stem (digest collision astronomically unlikely).
    assert _safe_cache_stem("totally-different") != stem

    # Empty / pathological input still produces a usable stem (non-empty).
    assert _safe_cache_stem("") != ""
    assert _safe_cache_stem("///") != ""
