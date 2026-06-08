"""Unit tests for `_reconcile_salary_for_write` (I-02 single-field inversion fix).

Regression target: a single-field salary enrichment (only salary_min OR only
salary_max in the UPDATE) left the unset column at its stored value, so a new
value inverting against the existing counterpart tripped the I-02 trigger
(tg_jobs_salary_range) and aborted the whole enrichment persist — silently
dropping jd_full's sibling fields (location) for that job.

The helper validates the EFFECTIVE pair the trigger sees and drops an inverting
incoming value rather than writing it. No DB access.
"""

from __future__ import annotations

from job_finder.db._jobs import _reconcile_salary_for_write as rec


def test_no_salary_supplied_is_noop():
    assert rec(None, None, 100_000, 200_000) == ({}, False)


# --- single-field: the bug scenario ----------------------------------------


def test_single_min_inverts_vs_existing_max_is_dropped():
    """New min above the stored max would trip I-02 → drop incoming, keep existing."""
    cols, dropped = rec(250_000, None, None, 200_000)
    assert dropped is True
    assert cols == {}  # nothing written → existing max preserved, trigger never fires


def test_single_max_inverts_vs_existing_min_is_dropped():
    cols, dropped = rec(None, 150_000, 200_000, None)
    assert dropped is True
    assert cols == {}


def test_single_min_valid_vs_existing_max_is_written():
    cols, dropped = rec(120_000, None, None, 200_000)
    assert dropped is False
    assert cols == {"salary_min": 120_000}


def test_single_max_valid_vs_existing_min_is_written():
    cols, dropped = rec(None, 200_000, 120_000, None)
    assert dropped is False
    assert cols == {"salary_max": 200_000}


def test_single_field_with_no_existing_counterpart_is_written():
    """No stored counterpart → no inversion possible → write as-is."""
    assert rec(150_000, None, None, None) == ({"salary_min": 150_000}, False)
    assert rec(None, 150_000, None, None) == ({"salary_max": 150_000}, False)


def test_real_world_inflated_min_against_good_existing_max():
    """The observed failure: ~100x-inflated incoming min vs a good stored max."""
    cols, dropped = rec(10_138_200, None, None, 209_296)
    assert dropped is True
    assert cols == {}  # good existing max=209296 preserved; persist no longer aborts


# --- both-field: preserves original _normalize_salary semantics --------------


def test_both_fields_valid_written_as_pair():
    assert rec(100_000, 200_000, None, None) == (
        {"salary_min": 100_000, "salary_max": 200_000},
        False,
    )


def test_both_fields_same_unit_inversion_swapped():
    cols, dropped = rec(200_000, 150_000, None, None)
    assert dropped is False
    assert cols == {"salary_min": 150_000, "salary_max": 200_000}


def test_both_fields_extreme_mismatch_dropped():
    cols, dropped = rec(10_138_200, 209_296, None, None)
    assert dropped is True
    assert cols == {}  # extreme: keep existing, write nothing
