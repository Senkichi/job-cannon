"""Tests for the low_signal classification rule (Phase 2d sub-fix).

5th classification value distinct from apply/consider/skip/reject.

Rule precedence in derive_classification:
    1. legitimacy_note truthy           -> 'reject'
    2. enrichment_tier == 'exhausted'
       AND jd_full_length < threshold   -> 'low_signal'
    3. any sub-score == 1               -> 'reject'
    4. all sub-scores >= 3              -> 'apply'
    5. all sub-scores >= 2              -> 'consider'
    6. otherwise                        -> 'skip'
"""

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
