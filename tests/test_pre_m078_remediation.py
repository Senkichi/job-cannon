"""Tests for scripts/pre_m078_remediation.py (Phase 47.03 / I-03 + I-13).

Covers the audit/remediate/verify contract:
  - I-03 heuristic-scored NULL-provider rows are backfilled to 'heuristic';
  - I-03 LLM-scored NULL-provider rows are skipped (different bug class) and
    remain genuine violators (m078 preflight would halt on them);
  - I-13 junk-jd_full rows are quarantined (jd_full -> NULL), not deleted;
  - clean rows are untouched;
  - unresolved_reasons append works post-m078 and is skipped pre-m078;
  - remediate is idempotent;
  - --verify exit codes; --audit never mutates.

Per the audit (§4.1) production has zero LLM-scored NULL-provider rows, so the
verify==0 / idempotency cases use production-shaped fixtures (heuristic
violators only). The LLM-skip behavior is exercised in its own test.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

# Load the script as a module (it lives under scripts/, not an importable package).
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pre_m078_remediation.py"
_spec = importlib.util.spec_from_file_location("pre_m078_remediation", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
remediation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(remediation)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_COLUMNS = """
    dedup_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    score REAL,
    scoring_provider TEXT,
    scoring_model TEXT,
    jd_full TEXT,
    enrichment_tier TEXT
"""

# A legitimate JD comfortably above the 200-char content-density floor.
_GOOD_JD = "Build and operate data products. " * 20  # ~640 chars


def _make_db(
    path: Path,
    *,
    with_unresolved_reasons: bool,
    with_llm_violator: bool = False,
) -> sqlite3.Connection:
    """Create a jobs table (pre- or post-m078 shape) and seed sample rows.

    Always seeds: one I-03 heuristic violator, two I-13 junk violators, and one
    clean row. When ``with_llm_violator`` is set, also seeds an LLM-scored row
    with a NULL provider — a genuine I-03 violator that remediate deliberately
    leaves for manual review.
    """
    cols = _BASE_COLUMNS
    if with_unresolved_reasons:
        cols += ",\n    unresolved_reasons TEXT NOT NULL DEFAULT '[]'"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE jobs ({cols})")

    rows = [
        # I-03 violator: heuristic-scored (scoring_model NULL), NULL provider.
        ("i03_heuristic", "Eng", "Acme", 50.0, None, None, _GOOD_JD, "static"),
        # I-13 violator: auth-wall shell pattern.
        ("i13_signin", "Eng", "Acme", None, None, None, "Sign in to view this job", "static"),
        # I-13 violator: SPA loading placeholder.
        ("i13_loading", "Eng", "Acme", None, None, None, "Loading...", "ai_navigate"),
        # Clean row: scored heuristic with a provider + a legit long JD.
        ("clean", "Eng", "Acme", 70.0, "heuristic", None, _GOOD_JD, "static"),
    ]
    if with_llm_violator:
        # LLM-scored, NULL provider: NOT auto-backfilled (different bug class).
        rows.append(("i03_llm", "Eng", "Acme", 50.0, None, "qwen2.5:14b", _GOOD_JD, "static"))

    conn.executemany(
        "INSERT INTO jobs (dedup_key, title, company, score, scoring_provider, "
        "scoring_model, jd_full, enrichment_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


@pytest.fixture
def db_pre_m078(tmp_path):
    conn = _make_db(tmp_path / "jobs.db", with_unresolved_reasons=False)
    yield conn
    conn.close()


@pytest.fixture
def db_post_m078(tmp_path):
    conn = _make_db(tmp_path / "jobs.db", with_unresolved_reasons=True)
    yield conn
    conn.close()


def _get(conn, dedup_key, column):
    row = conn.execute(f"SELECT {column} FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# count_violators / audit
# ---------------------------------------------------------------------------


def test_count_violators(db_pre_m078):
    i03, i13 = remediation.count_violators(db_pre_m078)
    assert i03 == 1  # i03_heuristic
    assert i13 == 2  # i13_signin + i13_loading


def test_count_violators_includes_llm_rows(tmp_path):
    # count_violators mirrors the m078 preflight: it counts ALL score+NULL-provider
    # rows, including LLM-scored ones that remediate won't auto-fix.
    conn = _make_db(tmp_path / "jobs.db", with_unresolved_reasons=False, with_llm_violator=True)
    try:
        i03, _ = remediation.count_violators(conn)
        assert i03 == 2  # i03_heuristic + i03_llm
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# remediate (pre-m078 — no unresolved_reasons column)
# ---------------------------------------------------------------------------


def test_remediate_pre_m078(db_pre_m078):
    summary = remediation.remediate(db_pre_m078)

    assert summary["i03_backfilled"] == 1
    assert summary["i13_quarantined"] == 2
    assert summary["i03_skipped_llm"] == 0

    # I-03 heuristic violator backfilled.
    assert _get(db_pre_m078, "i03_heuristic", "scoring_provider") == "heuristic"
    # I-13 violators have jd_full NULLed and enrichment_tier reset.
    assert _get(db_pre_m078, "i13_signin", "jd_full") is None
    assert _get(db_pre_m078, "i13_signin", "enrichment_tier") == "exhausted"
    assert _get(db_pre_m078, "i13_loading", "jd_full") is None
    # Clean row untouched.
    assert _get(db_pre_m078, "clean", "scoring_provider") == "heuristic"
    assert _get(db_pre_m078, "clean", "jd_full") == _GOOD_JD

    # Production-shaped DB (no LLM violators): fully drained.
    assert remediation.count_violators(db_pre_m078) == (0, 0)


def test_remediate_skips_llm_scored_row(tmp_path):
    conn = _make_db(tmp_path / "jobs.db", with_unresolved_reasons=False, with_llm_violator=True)
    try:
        summary = remediation.remediate(conn)
        assert summary["i03_backfilled"] == 1
        assert summary["i03_skipped_llm"] == 1
        # LLM-scored row left untouched — still a genuine I-03 violator.
        assert _get(conn, "i03_llm", "scoring_provider") is None
        i03, i13 = remediation.count_violators(conn)
        assert i03 == 1  # the un-backfilled LLM row remains; m078 would halt on it
        assert i13 == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# remediate (post-m078 — unresolved_reasons column present)
# ---------------------------------------------------------------------------


def test_remediate_post_m078_appends_reason(db_post_m078):
    remediation.remediate(db_post_m078)

    for key in ("i13_signin", "i13_loading"):
        reasons = json.loads(_get(db_post_m078, key, "unresolved_reasons"))
        assert reasons == ["jd_full_junk_pre_m078"]

    # Clean + I-03 rows keep their default empty reason list.
    assert _get(db_post_m078, "clean", "unresolved_reasons") == "[]"
    assert _get(db_post_m078, "i03_heuristic", "unresolved_reasons") == "[]"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_remediate_idempotent(db_post_m078):
    remediation.remediate(db_post_m078)
    second = remediation.remediate(db_post_m078)

    assert second["i03_backfilled"] == 0
    assert second["i13_quarantined"] == 0
    assert remediation.count_violators(db_post_m078) == (0, 0)

    # Reason code is not duplicated on re-run.
    reasons = json.loads(_get(db_post_m078, "i13_signin", "unresolved_reasons"))
    assert reasons == ["jd_full_junk_pre_m078"]


# ---------------------------------------------------------------------------
# CLI exit codes via main()
# ---------------------------------------------------------------------------


def test_main_verify_exit_codes(tmp_path):
    db_file = tmp_path / "jobs.db"
    conn = _make_db(db_file, with_unresolved_reasons=False)
    conn.close()

    # Un-remediated DB → verify exits 1.
    assert remediation.main(["--verify", "--db", str(db_file)]) == 1

    # Remediate, then verify exits 0.
    assert remediation.main(["--remediate", "--db", str(db_file)]) == 0
    assert remediation.main(["--verify", "--db", str(db_file)]) == 0


def test_main_missing_db_exits_2(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    assert remediation.main(["--audit", "--db", str(missing)]) == 2


def _snapshot(db_file: Path):
    conn = sqlite3.connect(str(db_file))
    try:
        return conn.execute(
            "SELECT dedup_key, scoring_provider, jd_full, enrichment_tier "
            "FROM jobs ORDER BY dedup_key"
        ).fetchall()
    finally:
        conn.close()


def test_audit_never_mutates(tmp_path):
    db_file = tmp_path / "jobs.db"
    conn = _make_db(db_file, with_unresolved_reasons=False)
    conn.close()

    before = _snapshot(db_file)
    assert remediation.main(["--audit", "--db", str(db_file)]) == 0
    after = _snapshot(db_file)

    # Audit is read-only: row contents are byte-for-byte unchanged.
    assert before == after
