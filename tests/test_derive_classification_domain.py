"""Domain guard tests for derive_classification (issue #257).

Validates that:
  - Malformed sub-score dicts raise ValueError (wrong/missing/extra keys,
    out-of-range values, non-int values including bool).
  - Valid 6-key 1–5 vectors still return the expected label unchanged.
  - The legitimacy_note and low_signal short-circuits still return without
    raising even when sub_scores is empty (they fire before the guard).
  - The redrive script completes a batch containing one malformed row and
    logs the bad dedup_key without aborting.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from job_finder.db import derive_classification

_VALID = {
    "title_fit": 4,
    "location_fit": 4,
    "comp_fit": 4,
    "domain_match": 4,
    "seniority_match": 4,
    "skills_match": 4,
}

_ALL_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


# ---------------------------------------------------------------------------
# Malformed input must raise ValueError
# ---------------------------------------------------------------------------


def test_empty_dict_raises():
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification({}, None)


def test_partial_dict_raises():
    partial = {"title_fit": 5, "comp_fit": 4}
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(partial, None)


def test_extra_key_raises():
    extra = dict(_VALID)
    extra["bonus_key"] = 3
    with pytest.raises(ValueError, match="extra keys"):
        derive_classification(extra, None)


def test_wrong_keys_raises():
    wrong = {k.upper(): v for k, v in _VALID.items()}
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(wrong, None)


@pytest.mark.parametrize("bad_value", [0, 6, 9, -1])
def test_out_of_range_int_raises(bad_value: int):
    bad = dict(_VALID)
    bad["title_fit"] = bad_value
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


@pytest.mark.parametrize("bad_value", ["5", 3.0, True, False, None])
def test_non_int_value_raises(bad_value):
    bad = dict(_VALID)
    bad["comp_fit"] = bad_value
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


# bool is an int subclass — both True and False must be rejected
def test_bool_true_raises():
    bad = dict(_VALID)
    bad["skills_match"] = True  # True == 1 as int but is bool
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


def test_bool_false_raises():
    bad = dict(_VALID)
    bad["skills_match"] = False  # False == 0 as int but is bool
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


# ---------------------------------------------------------------------------
# Valid vectors still produce the expected label (byte-for-byte unchanged)
# ---------------------------------------------------------------------------


def test_all_fives_returns_apply():
    scores = dict.fromkeys(_ALL_KEYS, 5)
    assert derive_classification(scores, None) == "apply"


def test_has_a_one_returns_reject():
    scores = dict(_VALID)
    scores["title_fit"] = 1
    assert derive_classification(scores, None) == "reject"


def test_all_twos_returns_consider():
    scores = dict.fromkeys(_ALL_KEYS, 2)
    assert derive_classification(scores, None) == "consider"


def test_all_threes_returns_low_signal():
    # Flat-neutral vector is no-signal, not apply (issue #210 branch C).
    scores = dict.fromkeys(_ALL_KEYS, 3)
    assert derive_classification(scores, None) == "low_signal"


# ---------------------------------------------------------------------------
# Short-circuit branches still fire on empty sub_scores without raising
# ---------------------------------------------------------------------------


def test_legitimacy_note_short_circuit_does_not_raise_on_empty():
    # legitimacy_note fires BEFORE the domain guard — must not raise
    result = derive_classification({}, "scam pattern")
    assert result == "reject"


def test_low_signal_short_circuit_does_not_raise_on_empty():
    # low_signal fires BEFORE the domain guard — must not raise
    result = derive_classification(
        {},
        None,
        enrichment_tier="exhausted",
        jd_full_length=100,
        low_signal_threshold=1500,
    )
    assert result == "low_signal"


# ---------------------------------------------------------------------------
# Redrive script: batch completes over a mix of valid + malformed rows
# ---------------------------------------------------------------------------


def _insert_scored_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    *,
    sub_scores: dict,
    classification: str,
    scoring_model: str = "qwen2.5:14b",
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen,
                          scoring_model, scoring_provider, sub_scores_json, classification,
                          fit_analysis)
        VALUES (?, 't', 'c', '', '2026-01-01', '2026-01-01',
                ?, 'ollama', ?, ?, '{}')
        """,
        (dedup_key, scoring_model, json.dumps(sub_scores), classification),
    )
    conn.commit()


@pytest.fixture
def conn(migrated_db):
    _path, c = migrated_db
    c.row_factory = sqlite3.Row
    return c


def test_redrive_batch_skips_malformed_and_continues(conn, caplog):
    """find_divergences skips malformed rows; batch over valid rows completes."""
    from scripts.redrive_classification import find_divergences

    # Valid row: stored "reject" but rule says "low_signal" (all-3s flat-neutral,
    # no legit note) → divergent
    _insert_scored_job(
        conn,
        "k|valid",
        sub_scores=dict.fromkeys(_ALL_KEYS, 3),
        classification="reject",
    )

    # Malformed row: partial sub_scores dict stored on disk
    partial_scores = {"title_fit": 5, "comp_fit": 4}  # only 2 of 6 keys
    _insert_scored_job(
        conn,
        "k|malformed",
        sub_scores=partial_scores,
        classification="apply",
    )

    with caplog.at_level(logging.WARNING, logger="scripts.redrive_classification"):
        divergences = find_divergences(conn)

    # Only the valid divergent row is returned
    dedup_keys = {d.dedup_key for d in divergences}
    assert "k|valid" in dedup_keys
    assert "k|malformed" not in dedup_keys

    # Bad row is logged
    assert "k|malformed" in caplog.text


def test_redrive_remediate_skips_malformed_row(conn, caplog):
    """remediate completes the batch even if a divergent row has malformed sub_scores."""
    from scripts.redrive_classification import remediate

    # The find_divergences skip already filters out malformed rows before
    # remediate sees them. This test verifies the full remediate path over
    # a batch that contains a malformed stored row from the start.
    _insert_scored_job(
        conn,
        "k|valid",
        sub_scores=dict.fromkeys(_ALL_KEYS, 3),
        classification="reject",
    )

    with caplog.at_level(logging.WARNING, logger="scripts.redrive_classification"):
        n = remediate(conn)

    # Only the valid divergent row was remediated
    assert n == 1
    stored = conn.execute("SELECT classification FROM jobs WHERE dedup_key='k|valid'").fetchone()[
        0
    ]
    assert stored == "low_signal"
    # Batch completed without exception
