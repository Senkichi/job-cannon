"""Collection-count sentinel — catches silent test-suite drift.

Reconciliation Plan v1 R2.3.

If pytest collection drops tests silently (skipif evaluating True when
it shouldn't, fixture-error swallowing a module, broken import that
pytest tolerates) this test fails loudly. Update EXPECTED_COLLECTED_FLOOR
DELIBERATELY when adding or removing tests.

Why this sentinel exists: at R0 audit (2026-05-06) the suite had 9
silent skips guarding code paths that, in 4 of 9 cases, were either
artifact-gated (F-C1), missing-package-gated (F-C1.5),
legitimately-obsolete (F-C1.6), statistical (F-C1.7), or user-config-gated
(F-C2). All four categories had been silent for an unknown duration,
giving false assurance that contracts were tested. The R2 work stripped
the silent skips. This sentinel keeps it that way.

How to update: when adding new tests, the floor moves UP (set it to the
new count minus a safety margin). When removing tests, the floor stays
the same and only moves down once the removal is intentional and committed.
A floor decrease MUST come with a commit message explaining why the
expected count dropped.

Calibration history:
  2026-05-06 — R2.3 first calibration: floor = 2130 against ~2139 collected
               (post-R2.1+R2.2 cleanup, pre-R2.5 surface tests). Margin: 9.
"""

EXPECTED_COLLECTED_FLOOR = 2190  # updated +63 tests: test_main_pidfile(13) + test_main_already_running(41) + test_jc_health_endpoint(9)

# Below this count we assume a subset run (single file, single test, --co
# subset) and the floor is not meaningful. The full suite collects ~2140;
# any subset under this threshold is plausibly a developer running a focused
# subset of tests during iteration.
_SUBSET_RUN_THRESHOLD = 500


def test_collection_floor_holds(request):
    """Asserts pytest collected at least EXPECTED_COLLECTED_FLOOR items.

    Only enforced on full-suite runs (collected count >= _SUBSET_RUN_THRESHOLD).
    Subset runs (single file, --tests, etc.) pass trivially because the floor
    is a full-suite invariant, not a per-run one.

    A failure on a full-suite run means one of:
      (1) tests were deleted intentionally — update EXPECTED_COLLECTED_FLOOR
          in the same commit as the deletion;
      (2) a test silently dropped from collection (skipif always-true,
          fixture import error, module-load failure tolerated by pytest) —
          investigate which file is missing and why;
      (3) the floor was set too high — calibrate down with explicit
          rationale in the commit message.
    """
    collected = getattr(request.config, "_collected_count", None)
    assert collected is not None, (
        "conftest.py's pytest_collection_modifyitems didn't record the count; "
        "see Reconciliation Plan v1 R2.3 for the contract."
    )
    if collected < _SUBSET_RUN_THRESHOLD:
        # Subset run — sentinel doesn't apply. Pass without skipping (skipping
        # would itself be a silent-skip, which this sentinel exists to prevent).
        return
    assert collected >= EXPECTED_COLLECTED_FLOOR, (
        f"Test collection dropped: got {collected}, expected >= "
        f"{EXPECTED_COLLECTED_FLOOR}. Either a test was removed "
        "(update EXPECTED_COLLECTED_FLOOR in the same commit) or a test "
        "is silently dropping from collection (investigate skipif decorators "
        "and fixture errors). See tests/test_collection_invariants.py docstring."
    )
