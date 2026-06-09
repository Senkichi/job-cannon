"""Tests for migration m078 — contract invariants (Phase 47.04).

Covers:
  - schema lands: unresolved_reasons column, 16 triggers, 1 unique index;
  - each trigger ABORTs a staged violating write (I-01..I-06, I-12, I-13);
  - I-04/I-05 gate on scoring_model (LLM-presence), not score — a heuristic-only
    row succeeds;
  - I-13 rejects each documented junk prefix + the length floor, accepts a
    legitimate JD;
  - I-11 partial unique index rejects duplicate (company_id, source_id) but
    allows repeated NULL source_id within a company;
  - upsert_job surfaces a trigger ABORT as IngestionRejected;
  - m078_down cleanly removes column, triggers, and index;
  - the preflight halts (and creates nothing) when violators pre-exist.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.migrations import MIGRATIONS, MigrationContext
from job_finder.web.migrations import m078_contract_invariants as m078
from job_finder.web.migrations._runner import _apply_migration

# A legitimate JD comfortably above the 200-char content-density floor.
_GOOD_JD = "Build and operate data products across the org. " * 8  # ~384 chars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_up_to(db_path: str, max_version: int) -> None:
    """Apply every migration with version <= max_version to db_path."""
    root = os.path.dirname(db_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=0)
        for migration in MIGRATIONS:
            if migration.version <= max_version:
                _apply_migration(ctx, migration)


def _head_conn(tmp_path) -> sqlite3.Connection:
    """A connection to a DB migrated to HEAD (m078 applied; triggers live)."""
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "head.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(conn: sqlite3.Connection, dedup_key: str, **cols) -> None:
    """Insert a jobs row with sensible defaults for the NOT NULL columns."""
    row = {
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-01-01T00:00:00",
    }
    row.update(cols)
    row["dedup_key"] = dedup_key
    keys = list(row)
    placeholders = ", ".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({placeholders})",
        [row[k] for k in keys],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Schema landing
# ---------------------------------------------------------------------------


def test_m078_creates_schema(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "unresolved_reasons" in cols

        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'tg_jobs_%'"
            ).fetchall()
        }
        assert len(triggers) == 16  # 8 invariants × (_ins + _upd)

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_jobs_company_source_id'"
            ).fetchall()
        }
        assert indexes == {"ix_jobs_company_source_id"}

        # unresolved_reasons defaults to '[]' for inserted rows.
        _insert(conn, "default_check", score=10.0, scoring_provider="heuristic")
        val = conn.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key='default_check'"
        ).fetchone()[0]
        assert val == "[]"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trigger enforcement
# ---------------------------------------------------------------------------


def test_i01_salary_min_positive(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-01"):
            _insert(conn, "i01", salary_min=0)
        with pytest.raises(sqlite3.IntegrityError, match="I-01"):
            _insert(conn, "i01b", salary_min=-5)
    finally:
        conn.close()


def test_i02_salary_range(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-02"):
            _insert(conn, "i02", salary_min=200000, salary_max=100000)
        # Equal min/max is fine.
        _insert(conn, "i02ok", salary_min=100000, salary_max=100000)
    finally:
        conn.close()


def test_i03_scoring_provider_required(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        # scoring_provider has a non-NULL column default, so the violation only
        # arises on an explicit NULL write (the F-03 regression shape).
        with pytest.raises(sqlite3.IntegrityError, match="I-03"):
            _insert(conn, "i03", score=50.0, scoring_provider=None)
        # Heuristic-scored row (provider set, no LLM fields) succeeds.
        _insert(conn, "i03ok", score=50.0, scoring_provider="heuristic")
    finally:
        conn.close()


def test_i04_subscores_required_when_llm_scored(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        # LLM-scored (scoring_model set) without sub_scores_json → I-04.
        # classification is supplied so I-05 passes and only I-04 fires.
        with pytest.raises(sqlite3.IntegrityError, match="I-04"):
            _insert(
                conn,
                "i04",
                score=50.0,
                scoring_provider="ollama",
                scoring_model="qwen2.5:14b",
                classification="consider",
            )
        # Heuristic-only row (scoring_model NULL) is unaffected by I-04.
        _insert(conn, "i04ok", score=50.0, scoring_provider="heuristic")
    finally:
        conn.close()


def test_i05_classification_required_when_llm_scored(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-05"):
            _insert(
                conn,
                "i05",
                score=50.0,
                scoring_provider="ollama",
                scoring_model="qwen2.5:14b",
                sub_scores_json="{}",
            )
        # With classification present, the LLM row is valid.
        _insert(
            conn,
            "i05ok",
            score=50.0,
            scoring_provider="ollama",
            scoring_model="qwen2.5:14b",
            sub_scores_json="{}",
            classification="consider",
        )
    finally:
        conn.close()


def test_i06_workplace_type_domain(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-06"):
            _insert(conn, "i06", workplace_type="remote")  # lowercase
        for valid in ("REMOTE", "HYBRID", "ONSITE", "UNSPECIFIED"):
            _insert(conn, f"i06ok_{valid}", workplace_type=valid)
    finally:
        conn.close()


def test_i12_posted_date_not_future(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-12"):
            _insert(conn, "i12", posted_date="2999-01-01T00:00:00")
        # A past date is fine.
        _insert(conn, "i12ok", posted_date="2020-01-01T00:00:00")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "junk",
    [
        "Sign in to continue",
        "Loading...",
        "Open roles at Acme",
        "Skip to content",
        "Cookie policy applies",
        "Privacy Policy notice",
        "404 not found",
        "too short",  # length floor
    ],
)
def test_i13_jd_full_junk(tmp_path, junk):
    conn = _head_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="I-13"):
            _insert(conn, "i13", jd_full=junk)
    finally:
        conn.close()


def test_i13_accepts_legit_jd(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        _insert(conn, "i13ok", jd_full=_GOOD_JD)
        assert len(_GOOD_JD) >= 200
    finally:
        conn.close()


def test_i11_company_source_id_unique(tmp_path):
    conn = _head_conn(tmp_path)
    try:
        _insert(conn, "i11a", company_id=1, source_id="abc")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "i11b", company_id=1, source_id="abc")
        # Different company, same source_id → allowed (namespaced by company).
        _insert(conn, "i11c", company_id=2, source_id="abc")
        # Repeated NULL source_id within a company → allowed (partial index).
        _insert(conn, "i11d", company_id=1, source_id=None)
        _insert(conn, "i11e", company_id=1, source_id=None)
        # Repeated '' source_id (the "no source_id" default) → also allowed:
        # the partial index excludes '' just like NULL.
        _insert(conn, "i11f", company_id=1, source_id="")
        _insert(conn, "i11g", company_id=1, source_id="")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# upsert_job → IngestionRejected
# ---------------------------------------------------------------------------


def test_upsert_job_raises_ingestion_rejected(tmp_path):
    from job_finder.db._jobs import IngestionRejected, upsert_job
    from job_finder.parsed_job import ParsedJob

    conn = _head_conn(tmp_path)
    try:
        # salary_min < 0 reaches the INSERT unchanged (max is None) → I-01 trigger.
        parsed = ParsedJob(
            title="Engineer",
            company="Acme",
            dedup_key="acme|engineer",
            salary_min=-5,
        )
        with pytest.raises(IngestionRejected) as exc:
            upsert_job(conn, parsed)
        assert exc.value.invariant == "I-01"
    finally:
        conn.close()


def test_upsert_job_merges_what_i11_raw_index_would_reject(tmp_path):
    """Sibling to test_i11_company_source_id_unique: at the raw-DB layer the
    I-11 partial UNIQUE index aborts the duplicate INSERT, but the upsert_job
    layer catches the (company_id, source_id) collision *before* it reaches
    that index and merges into the existing row instead (Issue #219).
    """
    from job_finder.db._jobs import IngestionRejected, upsert_job
    from job_finder.normalizers import normalize_company, normalize_title
    from job_finder.parsed_job import ParsedJob

    def _dk(company: str, title: str) -> str:
        return f"{normalize_company(company)}|{normalize_title(title)}"

    conn = _head_conn(tmp_path)
    try:
        parsed_a = ParsedJob(
            title="Senior Analyst",
            company="WorkdayCorp",
            dedup_key=_dk("WorkdayCorp", "Senior Analyst"),
            sources=["greenhouse"],
            source_urls=["https://example.com/a"],
            source_id="/job/Path/R-42",
        )
        r1 = upsert_job(conn, parsed_a, company_id=7)
        assert r1.kind == "inserted"

        # Distinct dedup_key, same (company_id, source_id) — at the raw-index
        # layer this would raise sqlite3.IntegrityError (see
        # test_i11_company_source_id_unique). Through upsert_job it merges.
        parsed_b = ParsedJob(
            title="Senior Manager",
            company="WorkdayCorp",
            dedup_key=_dk("WorkdayCorp", "Senior Manager"),
            sources=["greenhouse"],
            source_urls=["https://example.com/b"],
            source_id="/job/Path/R-42",
        )
        try:
            r2 = upsert_job(conn, parsed_b, company_id=7)
        except IngestionRejected as e:
            pytest.fail(
                f"upsert_job should merge on (company_id, source_id) collision, "
                f"not raise IngestionRejected: {e}"
            )
        assert r2.kind in {"updated", "touched", "unchanged"}
        assert r2.dedup_key == parsed_a.dedup_key

        # Exactly one row carries this source_id within the company.
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = 7 AND source_id = ?",
            (parsed_a.source_id,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# m078_down
# ---------------------------------------------------------------------------


def test_m078_down_removes_everything(tmp_path):
    db_path = str(tmp_path / "head.db")
    from job_finder.web.db_migrate import run_migrations

    run_migrations(db_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=str(tmp_path), initial_version=78
        )
        m078.m078_down(ctx)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "unresolved_reasons" not in cols
        triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'tg_jobs_%'"
        ).fetchone()[0]
        assert triggers == 0
        idx = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='ix_jobs_company_source_id'"
        ).fetchone()[0]
        assert idx == 0


# ---------------------------------------------------------------------------
# Preflight halt
# ---------------------------------------------------------------------------


def test_preflight_halts_on_violator_and_creates_nothing(tmp_path):
    db_path = str(tmp_path / "pre.db")
    _apply_up_to(db_path, 77)  # everything before m078

    # Stage an I-13 violator (no triggers yet, so the insert succeeds).
    with standalone_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, jd_full) "
            "VALUES ('junk', 'Eng', 'Acme', 'Remote', '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:00', 'Sign in to view')"
        )
        conn.commit()

    # Apply m078 directly; preflight must halt before any DDL.
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=str(tmp_path), initial_version=77
        )
        with pytest.raises(RuntimeError, match="pre_m078_remediation"):
            m078._migrate(ctx)

        # Nothing was created.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "unresolved_reasons" not in cols
        triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'tg_jobs_%'"
        ).fetchone()[0]
        assert triggers == 0
        idx = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='ix_jobs_company_source_id'"
        ).fetchone()[0]
        assert idx == 0
