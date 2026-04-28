"""Tests for job_finder.eval.metrics — hand-computed expected values.

Each metric is tested in isolation. Where a literature-defined metric
has a known reference implementation (ICC via pingouin, QWK via sklearn),
the assertion is intentionally a tolerance band rather than an exact
value: the goal is "we agree on order of magnitude and direction," not
"we replicate scipy's float bits."
"""

from __future__ import annotations

import math

import pytest

from job_finder.eval.metrics import (
    apply_false_positive_rate,
    bias,
    bootstrap_ci,
    classification_metrics,
    coherence_violations,
    confusion_matrix,
    icc,
    mae,
    qw_kappa,
)

# -----------------------------------------------------------------------------
# mae / bias
# -----------------------------------------------------------------------------


def test_mae_equal_arrays_is_zero():
    assert mae([1, 2, 3], [1, 2, 3]) == 0.0


def test_mae_known_values():
    assert mae([1, 2, 3], [2, 4, 6]) == pytest.approx(2.0)


def test_mae_handles_empty_lists():
    assert math.isnan(mae([], []))


def test_mae_length_mismatch_raises():
    with pytest.raises(ValueError):
        mae([1, 2], [1, 2, 3])


def test_bias_signed_positive_means_over_prediction():
    assert bias(y_true=[1, 2, 3], y_pred=[2, 3, 4]) == pytest.approx(1.0)


def test_bias_signed_negative_means_under_prediction():
    assert bias(y_true=[3, 3, 3], y_pred=[1, 2, 3]) == pytest.approx(-1.0)


def test_bias_handles_empty_lists():
    assert math.isnan(bias([], []))


# -----------------------------------------------------------------------------
# icc
# -----------------------------------------------------------------------------


def test_icc_perfect_agreement_is_one():
    raters = [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5]]
    assert icc(raters) == pytest.approx(1.0)


def test_icc_systematic_bias_is_low():
    """Constant +4 bias with no within-subject variance => ICC ≈ 0."""
    raters = [[1, 1, 1, 1], [5, 5, 5, 5]]
    val = icc(raters)
    # No variability across subjects on either rater => ICC undefined or near 0.
    assert math.isnan(val) or val < 0.1


def test_icc_moderate_agreement_in_expected_band():
    """Hand cross-checked against pingouin.intraclass_corr (ICC2)."""
    raters = [[3, 3, 4, 5, 4], [4, 3, 4, 5, 5]]
    val = icc(raters)
    assert 0.55 <= val <= 0.95


def test_icc_too_few_raters_returns_nan():
    assert math.isnan(icc([[1, 2, 3]]))


def test_icc_too_few_subjects_returns_nan():
    assert math.isnan(icc([[1], [1]]))


# -----------------------------------------------------------------------------
# qw_kappa
# -----------------------------------------------------------------------------


def test_qw_kappa_perfect_agreement_is_one():
    val = qw_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], min_rating=1, max_rating=5)
    assert val == pytest.approx(1.0)


def test_qw_kappa_inverse_disagreement_is_negative():
    val = qw_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], min_rating=1, max_rating=5)
    assert val < 0


def test_qw_kappa_off_by_two_worse_than_off_by_one():
    one_off = qw_kappa([1, 2, 3, 4, 5], [2, 3, 4, 5, 5], min_rating=1, max_rating=5)
    two_off = qw_kappa([1, 2, 3, 4, 5], [3, 4, 5, 5, 5], min_rating=1, max_rating=5)
    assert one_off > two_off


def test_qw_kappa_empty_returns_nan():
    assert math.isnan(qw_kappa([], [], 1, 5))


# -----------------------------------------------------------------------------
# bootstrap_ci
# -----------------------------------------------------------------------------


def test_bootstrap_ci_brackets_sample_mean():
    data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    lo, hi = bootstrap_ci(
        data,
        statistic=lambda xs: sum(xs) / len(xs),
        n_resamples=1000,
        ci=0.95,
        seed=42,
    )
    assert lo < 5.5 < hi


def test_bootstrap_ci_constant_input_collapses():
    data = [3, 3, 3, 3, 3]
    lo, hi = bootstrap_ci(
        data,
        statistic=lambda xs: sum(xs) / len(xs),
        n_resamples=500,
        ci=0.95,
        seed=42,
    )
    assert lo == pytest.approx(3.0)
    assert hi == pytest.approx(3.0)


def test_bootstrap_ci_paired_difference():
    """All deltas = +1 => CI collapses to (1.0, 1.0)."""
    baseline = [3, 3, 3, 3, 3]
    candidate = [4, 4, 4, 4, 4]
    deltas = [c - b for c, b in zip(candidate, baseline, strict=True)]
    lo, hi = bootstrap_ci(
        deltas,
        statistic=lambda xs: sum(xs) / len(xs),
        n_resamples=500,
        ci=0.95,
        seed=42,
    )
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(1.0)


def test_bootstrap_ci_empty_input_returns_nan():
    lo, hi = bootstrap_ci([], statistic=sum)
    assert math.isnan(lo) and math.isnan(hi)


# -----------------------------------------------------------------------------
# confusion_matrix
# -----------------------------------------------------------------------------


def test_confusion_matrix_5x5_diagonals_and_offdiagonals():
    classes = ("apply", "consider", "skip", "reject", "low_signal")
    y_true = ["apply", "apply", "consider", "skip", "reject"]
    y_pred = ["apply", "consider", "consider", "apply", "reject"]
    cm = confusion_matrix(y_true, y_pred, classes)
    assert cm["apply"]["apply"] == 1
    assert cm["apply"]["consider"] == 1
    assert cm["consider"]["consider"] == 1
    assert cm["skip"]["apply"] == 1
    assert cm["reject"]["reject"] == 1


def test_confusion_matrix_drops_unknown_labels():
    classes = ("apply", "skip")
    cm = confusion_matrix(["apply", "??"], ["apply", "??"], classes)
    assert cm["apply"]["apply"] == 1
    # No row/col was added for "??"
    assert "??" not in cm


# -----------------------------------------------------------------------------
# classification_metrics + apply_false_positive_rate
# -----------------------------------------------------------------------------


def test_classification_metrics_per_class_and_macro():
    classes = ("apply", "consider", "skip", "reject", "low_signal")
    y_true = ["apply"] * 4 + ["consider"] * 4 + ["reject"] * 2
    y_pred = ["apply"] * 3 + ["consider"] * 1 + ["consider"] * 4 + ["reject"] * 2
    m = classification_metrics(y_true, y_pred, classes)
    # apply: TP=3, FP=0, FN=1
    assert m["apply"]["precision"] == pytest.approx(1.0)
    assert m["apply"]["recall"] == pytest.approx(0.75)
    assert m["apply"]["f1"] == pytest.approx(2 * 1.0 * 0.75 / (1.0 + 0.75))
    # macro_f1 averages over all 5 classes (skip / low_signal contribute 0)
    assert 0.0 < m["macro_f1"] < 1.0


def test_apply_false_positive_rate_when_all_non_apply_predicted_apply():
    y_true = ["apply", "consider", "consider", "skip"]
    y_pred = ["apply", "apply", "apply", "apply"]
    assert apply_false_positive_rate(y_true, y_pred) == pytest.approx(1.0)


def test_apply_false_positive_rate_returns_nan_when_all_truths_are_apply():
    assert math.isnan(apply_false_positive_rate(["apply", "apply"], ["apply", "skip"]))


# -----------------------------------------------------------------------------
# coherence_violations
# -----------------------------------------------------------------------------


def test_coherence_violation_flagged_when_high_score_contradicts_gap():
    rows = [
        {
            "sub_scores": {
                "title_fit": 5,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "gaps_text": "title mismatch — wrong function",
        },
    ]
    violations = coherence_violations(rows)
    assert len(violations) == 1
    assert violations[0]["axis"] == "title_fit"


def test_no_coherence_violation_when_gap_consistent_with_low_score():
    rows = [
        {
            "sub_scores": {
                "title_fit": 2,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "gaps_text": "title mismatch — wrong function",
        },
    ]
    assert coherence_violations(rows) == []


def test_coherence_violations_handles_missing_gaps_and_scores():
    rows = [
        {"sub_scores": {"title_fit": 5}, "gaps_text": ""},
        {"sub_scores": {}, "gaps_text": "title mismatch"},
        {"sub_scores": {"title_fit": "not-a-number"}, "gaps_text": "title mismatch"},
    ]
    assert coherence_violations(rows) == []


def test_coherence_violation_one_per_row_first_match_wins():
    """Two axes both contradict; assert exactly one violation per row."""
    rows = [
        {
            "sub_scores": {
                "title_fit": 5,
                "location_fit": 5,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "gaps_text": "title mismatch and location is wrong",
        }
    ]
    assert len(coherence_violations(rows)) == 1
