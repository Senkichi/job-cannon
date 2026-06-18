"""Tests for Phase 49.04 — scripts/redrive_classification.py reconciliation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.redrive_classification import find_divergences, remediate

# Strong, non-flat vector that derives to "apply" under the positive-evidence
# rule (issue #210): mean 4.0, 6 strong axes. An all-3s vector would now derive
# to "low_signal", which would defeat these apply-vs-drift fixtures.
_APPLY_SUB_SCORES = {
    "title_fit": 4,
    "location_fit": 4,
    "comp_fit": 4,
    "domain_match": 4,
    "seniority_match": 4,
    "skills_match": 4,
}


def _insert_scored_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    *,
    sub_scores: dict,
    classification: str,
    enrichment_tier: str | None = None,
    jd_full: str | None = "x" * 3000,
    scoring_model: str = "qwen2.5:14b",
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen,
                          scoring_model, scoring_provider, sub_scores_json, classification,
                          fit_analysis, enrichment_tier, jd_full)
        VALUES (?, 't', 'c', '', '2026-01-01', '2026-01-01',
                ?, 'anthropic', ?, ?, '{}', ?, ?)
        """,
        (
            dedup_key,
            scoring_model,
            json.dumps(sub_scores),
            classification,
            enrichment_tier,
            jd_full,
        ),
    )
    conn.commit()


@pytest.fixture
def conn(migrated_db):
    _path, c = migrated_db
    c.row_factory = sqlite3.Row
    return c


def test_audit_reports_divergence_without_writing(conn):
    # enrichment exhausted + short jd → rule says low_signal; stored is stale 'reject'.
    _insert_scored_job(
        conn,
        "k|stale",
        sub_scores=_APPLY_SUB_SCORES,
        classification="reject",
        enrichment_tier="exhausted",
        jd_full=None,  # NULL jd → jd_len 0 < threshold → low_signal (I-13 allows NULL)
    )
    divergences = find_divergences(conn)
    assert len(divergences) == 1
    assert divergences[0].stored == "reject"
    assert divergences[0].computed == "low_signal"
    # audit must not write
    stored = conn.execute("SELECT classification FROM jobs WHERE dedup_key='k|stale'").fetchone()[
        0
    ]
    assert stored == "reject"


def test_remediate_reconciles_to_zero(conn):
    _insert_scored_job(
        conn,
        "k|stale",
        sub_scores=_APPLY_SUB_SCORES,
        classification="reject",
        enrichment_tier="exhausted",
        jd_full=None,  # NULL jd → jd_len 0 < threshold → low_signal (I-13 allows NULL)
    )
    n = remediate(conn)
    assert n == 1
    assert find_divergences(conn) == []
    stored = conn.execute("SELECT classification FROM jobs WHERE dedup_key='k|stale'").fetchone()[
        0
    ]
    assert stored == "low_signal"


def test_remediate_is_idempotent(conn):
    _insert_scored_job(
        conn,
        "k|stale",
        sub_scores=_APPLY_SUB_SCORES,
        classification="reject",
        enrichment_tier="exhausted",
        jd_full=None,  # NULL jd → jd_len 0 < threshold → low_signal (I-13 allows NULL)
    )
    assert remediate(conn) == 1
    assert remediate(conn) == 0  # second run: nothing divergent


def test_non_llm_rows_are_ignored(conn):
    # scoring_model NULL → not LLM-scored → excluded from redrive even if divergent.
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen,
                          scoring_model, sub_scores_json, classification, enrichment_tier, jd_full)
        VALUES ('k|heur', 't', 'c', '', '2026-01-01', '2026-01-01',
                NULL, ?, 'reject', 'exhausted', NULL)
        """,
        (json.dumps(_APPLY_SUB_SCORES),),
    )
    conn.commit()
    assert find_divergences(conn) == []


def test_config_threshold_surfaces_new_drift(conn):
    # jd_len 1600: default threshold 1500 → not low_signal (apply matches, no drift).
    # config threshold 2000 → 1600 < 2000 → low_signal → drift vs stored 'apply'.
    _insert_scored_job(
        conn,
        "k|thresh",
        sub_scores=_APPLY_SUB_SCORES,
        classification="apply",
        enrichment_tier="exhausted",
        jd_full="x" * 1600,
    )
    assert find_divergences(conn, config=None) == []
    drift = find_divergences(conn, config={"scoring": {"low_signal_jd_chars": 2000}})
    assert len(drift) == 1
    assert drift[0].computed == "low_signal"


def test_writer_relocation_keeps_import_paths():
    # persist_job_assessment is importable from both the new module and the
    # back-compat shim (Phase 49.04 relocation).
    from job_finder.db import persist_job_assessment as from_pkg
    from job_finder.db._assessment_writer import persist_job_assessment as from_new
    from job_finder.db._persistence import persist_job_assessment as from_shim

    assert from_pkg is from_new is from_shim
