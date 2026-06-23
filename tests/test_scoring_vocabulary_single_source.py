"""Guard: the scoring vocabulary has ONE source of truth.

The six ordinal axes and the five classification verdicts used to be copy-pasted
across db/, web/, eval/, and scripts/. They now derive from
job_finder.constants.{SUB_SCORE_KEYS, CLASSIFICATIONS}. This test fails loudly if
any consumer reintroduces a divergent literal — the "fully-written-but-silently-
out-of-sync list" failure mode this refactor exists to kill.

The frozen production schema (v3_scoring_prompt.JOB_ASSESSMENT_SCHEMA) is
deliberately NOT derived (it must stay byte-stable for eval reproducibility,
per its module docstring), so it is *pinned* here instead: if the canonical axis
list ever changes, this test forces a conscious reconciliation with the freeze.
"""

from __future__ import annotations

from job_finder.constants import CLASSIFICATIONS, SUB_SCORE_KEYS


def test_axis_consumers_derive_from_canonical():
    """Every module-level axis alias is the canonical tuple, not a copy."""
    from job_finder.db._classification import _SUB_SCORE_KEYS
    from job_finder.eval import harness, report
    from job_finder.scripts import label_gold_set
    from job_finder.web import job_scorer, model_provider

    expected_axis_set = frozenset(SUB_SCORE_KEYS)
    assert _SUB_SCORE_KEYS == SUB_SCORE_KEYS
    assert job_scorer._SUB_SCORE_KEYS == SUB_SCORE_KEYS
    assert expected_axis_set == model_provider._SCORING_AXIS_KEYS
    assert harness.AXES == SUB_SCORE_KEYS
    assert report.AXES == SUB_SCORE_KEYS
    assert label_gold_set.SUB_SCORE_AXES == SUB_SCORE_KEYS


def test_classification_consumers_derive_from_canonical():
    """Every module-level verdict alias is the canonical tuple, not a copy."""
    from job_finder.eval import harness
    from job_finder.scripts import label_gold_set

    assert harness.CLASSES == CLASSIFICATIONS
    assert label_gold_set.VALID_CLASSIFICATIONS == CLASSIFICATIONS


def test_axis_keyword_table_keys_match_canonical():
    """eval.metrics.AXIS_KEYWORDS keeps per-axis data, but its KEYS must be the
    canonical axis set (a missing/renamed axis would silently skip coherence
    checks for that axis)."""
    from job_finder.eval.metrics import AXIS_KEYWORDS

    assert set(AXIS_KEYWORDS) == set(SUB_SCORE_KEYS)


def test_frozen_v3_schema_axes_pinned_to_canonical():
    """The FROZEN production schema is not derived, so pin its axis keys here.

    A divergence means either the schema thawed or the canonical list moved —
    both demand a deliberate decision, not a silent mismatch between what the
    LLM is asked for and what the rest of the system enumerates.
    """
    from job_finder.web.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA

    props = JOB_ASSESSMENT_SCHEMA["properties"]
    schema_axis_keys = {k for k, v in props.items() if v.get("type") == "integer"}
    assert schema_axis_keys == set(SUB_SCORE_KEYS)
    # Every axis is also a required top-level field.
    assert set(SUB_SCORE_KEYS).issubset(set(JOB_ASSESSMENT_SCHEMA["required"]))
    # Each axis carries the uniform 1-5 ordinal constraint.
    for axis in SUB_SCORE_KEYS:
        assert props[axis] == {"type": "integer", "minimum": 1, "maximum": 5}
