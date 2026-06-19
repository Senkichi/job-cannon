"""Tests for the v3_1 scoring-prompt variant (P3.3, eval-gated per D-10).

Contract:
  - exports the four required names (the variants-loadable contract);
  - reuses v3.0's frozen schema / field-reinforcement / few-shots UNCHANGED
    (D-10 freeze discipline — only the header diverges);
  - header defines 2 and 4 anchors on every axis;
  - header carries the no-signal instruction (absence of evidence ≠ neutral);
  - header does NOT introduce a 0 abstain code (schema stays 1-5 — divergence
    from the v4b1 prototype);
  - resolves through the production job_scorer resolver.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts import v3_scoring_prompt as v3
from job_finder.web.scoring_prompts.variants import v3_1

_AXES = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def test_exports_required_names():
    for name in (
        "V3_SCORING_PROMPT",
        "V3_SCORING_PROMPT_HEADER",
        "FIELD_REINFORCEMENT",
        "FEWSHOT_EXAMPLES",
        "JOB_ASSESSMENT_SCHEMA",
    ):
        assert hasattr(v3_1, name), f"v3_1 missing {name!r}"


def test_schema_unchanged_from_v3():
    """Non-goal: do not change the six-axis schema. v3_1 reuses v3's object."""
    assert v3_1.JOB_ASSESSMENT_SCHEMA is v3.JOB_ASSESSMENT_SCHEMA


def test_field_reinforcement_and_fewshots_unchanged_from_v3():
    assert v3_1.FIELD_REINFORCEMENT is v3.FIELD_REINFORCEMENT
    assert v3_1.FEWSHOT_EXAMPLES is v3.FEWSHOT_EXAMPLES


def test_header_diverges_from_v3():
    assert v3_1.V3_SCORING_PROMPT_HEADER != v3.V3_SCORING_PROMPT_HEADER


def test_every_axis_has_2_and_4_anchors():
    header = v3_1.V3_SCORING_PROMPT_HEADER
    # Each axis section must define all five score anchors.
    for n in (1, 2, 3, 4, 5):
        token = f"score of {n}:"
        # at least six occurrences (one per axis) for 2 and 4 specifically.
        assert header.count(token) >= 6, f"missing enough 'score of {n}:' anchors"


def test_each_axis_section_present():
    header = v3_1.V3_SCORING_PROMPT_HEADER
    for axis in _AXES:
        assert axis in header, f"axis {axis} not documented in header"


def test_no_signal_instruction_present():
    header = v3_1.V3_SCORING_PROMPT_HEADER
    assert "Absence of evidence is NOT neutral evidence" in header
    # The phrase the spec requires: absence-of-evidence falls to a stated default.
    assert "no-signal default" in header
    # location_fit's no-signal default is the documented S6 fix (2, not 3).
    assert "no-signal default: 2" in header


def test_no_zero_abstain_code():
    """v3_1 keeps the frozen 1-5 schema — it must NOT add a 0 code (vs v4b1)."""
    header = v3_1.V3_SCORING_PROMPT_HEADER
    assert "0 — NO SIGNAL" not in header
    assert "score of 0" not in header
    # Schema minimum stays 1 on every axis.
    for axis in _AXES:
        assert v3_1.JOB_ASSESSMENT_SCHEMA["properties"][axis]["minimum"] == 1


def test_header_references_location_facts_block():
    header = v3_1.V3_SCORING_PROMPT_HEADER
    assert "Location facts:" in header
    assert "candidate-geography-match" in header


def test_resolves_through_job_scorer():
    from job_finder.web.job_scorer import _resolve_schema, _resolve_variant_module

    mod = _resolve_variant_module("v3_1")
    assert mod is v3_1
    cfg = {"scoring": {"prompt_variant": "v3_1"}}
    assert _resolve_schema(cfg) is v3.JOB_ASSESSMENT_SCHEMA


def test_build_system_prompt_uses_v3_1_header():
    from job_finder.web.job_scorer import _build_system_prompt

    cfg = {"scoring": {"prompt_variant": "v3_1"}}
    prompt = _build_system_prompt(candidate_context="CTX_MARKER", config=cfg)
    assert "Absence of evidence is NOT neutral evidence" in prompt
    assert "CTX_MARKER" in prompt
    assert "Fewshot calibration examples" in prompt
