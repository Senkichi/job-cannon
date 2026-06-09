"""Tests for the low_signal classification rule (Phase 2d sub-fix).

5th classification value distinct from apply/consider/skip/reject.

Rule precedence in derive_classification:
    1. legitimacy_note truthy           -> 'reject'
    2. enrichment_tier in terminal set
       AND jd_full_length < threshold   -> 'low_signal'
    3. any sub-score == 1               -> 'reject'
    4. all sub-scores >= 3              -> 'apply'
    5. all sub-scores >= 2              -> 'consider'
    6. otherwise                        -> 'skip'

Terminal tiers (no further automatic enrichment): 'exhausted', 'agentic',
'agentic_exhausted' (see _TERMINAL_ENRICHMENT_TIERS in _classification.py).
"""

import pytest

from job_finder.db import derive_classification


def test_low_signal_when_exhausted_and_short_jd():
    sub_scores = {
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


def test_not_low_signal_when_jd_long_enough():
    sub_scores = {
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=5000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


def test_not_low_signal_when_enrichment_not_exhausted():
    """Short JD with enrichment_tier=None is a re-enrichment candidate, not low_signal."""
    sub_scores = {
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    # Standard rule applies; enrichment can still run on a future tier.
    assert cls == "apply"


def test_legitimacy_note_overrides_low_signal():
    sub_scores = dict.fromkeys(
        (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ),
        3,
    )
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note="scam pattern",
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "reject"


def test_any_axis_one_after_low_signal_check_does_not_promote():
    """Rule order: legitimacy → low_signal → any-1-reject. low_signal wins over any-1.

    This is intentional per spec D-2.5: a job with insufficient JD signal cannot
    be confidently rejected on rubric outputs (the 1 may itself be a hallucination
    from the model trying to score against an empty prompt). Surfacing low_signal
    is the honest answer.
    """
    sub_scores = {
        "title_fit": 1,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


# --- Coverage for the terminal-tier set (issue #225) -------------------------
#
# The low_signal rule must fire for every tier from which no further automatic
# enrichment will run. The agentic enricher flips 'exhausted' to 'agentic' on
# success and 'agentic_exhausted' on failure, both of which are also terminal.

_TERMINAL_TIERS = ("exhausted", "agentic", "agentic_exhausted")


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_low_signal_fires_for_terminal_tier_with_short_jd(tier):
    sub_scores = dict.fromkeys(
        (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ),
        3,
    )
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_terminal_tier_short_jd_with_hallucinated_one_is_low_signal_not_reject(tier):
    """Acceptance criterion: a short-JD terminal-tier row with a hallucinated 1
    sub-score must classify as low_signal, NOT reject. The low_signal branch
    sits before the any-axis-1 reject precisely to absorb rubric noise on
    insufficient-context jobs.
    """
    sub_scores = {
        "title_fit": 1,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_terminal_tier_long_jd_classifies_on_normal_rubric(tier):
    """Long-JD rows at a terminal tier follow the standard rubric (not low_signal)."""
    sub_scores = dict.fromkeys(
        (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ),
        3,
    )
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=5000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


@pytest.mark.parametrize("tier", ("free", "ddg", "low", "serpapi", "mid"))
def test_non_terminal_tier_short_jd_is_not_low_signal(tier):
    """Non-terminal tiers with short JDs are re-enrichment candidates and must
    not be diverted to low_signal — preserves the original
    test_not_low_signal_when_enrichment_not_exhausted contract across the
    full non-terminal vocabulary.
    """
    sub_scores = dict.fromkeys(
        (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ),
        3,
    )
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "apply"
