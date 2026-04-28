"""Pure metric functions for the eval harness (Phase 5).

All functions accept lists or numpy arrays of equal length. Edge cases
(empty input, length mismatch in some places) return float('nan') for
MAE / bias / ICC / kappa; for set-based or count-based metrics, return
0.0 or empty result. No I/O, no DB, no LLM calls — these are deliberately
side-effect-free so they can be reasoned about and tested in isolation.

Per spec D-5.5, the coherence metric is a keyword-overlap heuristic
(simple-first); embedding-based variants are deferred until the gold
set is large enough to justify the cost.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence

# -----------------------------------------------------------------------------
# Per-axis numeric metrics
# -----------------------------------------------------------------------------


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean absolute error."""
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch")
    if not y_true:
        return float("nan")
    return sum(abs(a - b) for a, b in zip(y_true, y_pred, strict=True)) / len(y_true)


def bias(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean signed error (y_pred - y_true). Positive = model over-predicts."""
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch")
    if not y_true:
        return float("nan")
    return sum(b - a for a, b in zip(y_true, y_pred, strict=True)) / len(y_true)


def icc(raters: Sequence[Sequence[float]]) -> float:
    """ICC(2,1) — two-way random effects, single-measurement, absolute agreement.

    raters: list of 2+ arrays of equal length, each containing one rater's
    scores across n_subjects subjects. Returns NaN for degenerate inputs.

    Penalizes systematic bias (unlike ICC(3,1) consistency), making it the
    right pick when "model agrees with human label" is the question.
    """
    import numpy as np

    a = np.array(raters, dtype=float)
    if a.ndim != 2 or a.shape[0] < 2 or a.shape[1] < 2:
        return float("nan")
    n_raters, n_subjects = a.shape
    grand_mean = a.mean()
    subject_means = a.mean(axis=0)
    rater_means = a.mean(axis=1)

    ss_between_subjects = n_raters * float(((subject_means - grand_mean) ** 2).sum())
    ms_between = ss_between_subjects / (n_subjects - 1)

    ss_between_raters = n_subjects * float(((rater_means - grand_mean) ** 2).sum())
    ms_rater = ss_between_raters / (n_raters - 1)

    ss_total = float(((a - grand_mean) ** 2).sum())
    ss_error = ss_total - ss_between_subjects - ss_between_raters
    df_error = (n_raters - 1) * (n_subjects - 1)
    if df_error <= 0:
        return float("nan")
    ms_error = ss_error / df_error

    denom = (
        ms_between + (n_raters - 1) * ms_error + (n_raters / n_subjects) * (ms_rater - ms_error)
    )
    if denom <= 0:
        return float("nan")
    return float((ms_between - ms_error) / denom)


def qw_kappa(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    min_rating: int = 1,
    max_rating: int = 5,
) -> float:
    """Quadratic-weighted Cohen's kappa.

    Penalizes off-by-N disagreements quadratically — the right kappa for
    ordinal rating scales (1-5 sub-scores). Equivalent to
    sklearn.metrics.cohen_kappa_score(weights='quadratic').
    """
    import numpy as np

    if len(y_true) != len(y_pred) or not y_true:
        return float("nan")
    n = max_rating - min_rating + 1
    if n < 2:
        return float("nan")
    obs = np.zeros((n, n))
    for t, p in zip(y_true, y_pred, strict=True):
        ti = int(t) - min_rating
        pi = int(p) - min_rating
        if 0 <= ti < n and 0 <= pi < n:
            obs[ti, pi] += 1
    total = obs.sum()
    if total == 0:
        return float("nan")
    hist_t = obs.sum(axis=1)
    hist_p = obs.sum(axis=0)
    expected = np.outer(hist_t, hist_p) / total
    weights = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            weights[i, j] = ((i - j) ** 2) / ((n - 1) ** 2)
    num = float((weights * obs).sum())
    den = float((weights * expected).sum())
    if den == 0:
        return float("nan")
    return float(1 - num / den)


# -----------------------------------------------------------------------------
# Bootstrap CI
# -----------------------------------------------------------------------------


def bootstrap_ci(
    data: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int | None = None,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a statistic. Returns (lo, hi)."""
    if not data:
        return (float("nan"), float("nan"))
    # S311: bootstrap resampling does not require a CSPRNG; deterministic seed
    # support is the property we care about, not cryptographic strength.
    rng = random.Random(seed) if seed is not None else random  # noqa: S311
    n = len(data)
    samples: list[float] = []
    for _ in range(n_resamples):
        resample = [data[rng.randrange(n)] for _ in range(n)]
        samples.append(statistic(resample))
    samples.sort()
    lo_idx = int((1 - ci) / 2 * n_resamples)
    hi_idx = int((1 + ci) / 2 * n_resamples)
    hi_idx = min(hi_idx, n_resamples)
    return (samples[lo_idx], samples[hi_idx - 1])


# -----------------------------------------------------------------------------
# Classification metrics
# -----------------------------------------------------------------------------


def confusion_matrix(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    classes: Sequence[str],
) -> dict:
    """Return cm[true_class][pred_class] = count. Out-of-vocab labels are dropped."""
    cm: dict = {a: dict.fromkeys(classes, 0) for a in classes}
    cls_set = set(classes)
    for t, p in zip(y_true, y_pred, strict=True):
        if t in cls_set and p in cls_set:
            cm[t][p] += 1
    return cm


def classification_metrics(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    classes: Sequence[str],
) -> dict:
    """Per-class precision/recall/F1 + macro F1.

    macro_f1 is computed across the explicit class list (not only classes
    with nonzero support) so a missing class drags the average down — we
    want to *see* that, not paper over it.
    """
    cm = confusion_matrix(y_true, y_pred, classes)
    out: dict = {}
    for c in classes:
        tp = cm[c][c]
        fp = sum(cm[other][c] for other in classes if other != c)
        fn = sum(cm[c][other] for other in classes if other != c)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        if (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0
        out[c] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": tp + fn,
        }
    out["macro_f1"] = sum(out[c]["f1"] for c in classes) / len(classes) if classes else 0.0
    return out


def apply_false_positive_rate(
    y_true: Sequence[str],
    y_pred: Sequence[str],
) -> float:
    """Fraction of non-apply truths predicted as apply.

    The headline business metric (spec D-5.3): the production cost of an
    apply-shaped recommendation that the user would not have applied to.
    """
    non_apply = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if t != "apply"]
    if not non_apply:
        return float("nan")
    fp = sum(1 for _, p in non_apply if p == "apply")
    return fp / len(non_apply)


# -----------------------------------------------------------------------------
# Coherence (rationale-vs-score consistency)
# -----------------------------------------------------------------------------


# Per axis, the keywords whose presence in gaps text is consistent only
# with a LOW score on that axis. If the model wrote one of these gaps
# but scored the axis ≥ HIGH_SCORE_THRESHOLD, that is a coherence
# violation — the rationale and the rubric output disagree.
AXIS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "title_fit": ("title", "role function", "wrong function"),
    "location_fit": (
        "location",
        "geography",
        "remote",
        "on-site",
        "relocation",
    ),
    "comp_fit": ("salary", "comp", "compensation", "pay"),
    "domain_match": ("industry", "vertical", "domain"),
    "seniority_match": (
        "seniority",
        "level",
        "junior",
        "senior",
        "experience years",
    ),
    "skills_match": ("skill", "technology", "stack"),
}
HIGH_SCORE_THRESHOLD = 4


def coherence_violations(rows: Sequence[dict]) -> list[dict]:
    """Detect rows whose gaps text mentions an axis but scored that axis high.

    Each row is a dict with:
        sub_scores: dict[axis_name, int]  — required for any check
        gaps_text:  str | None            — joined gaps from rationale

    Returns one violation per offending row (first match wins) so the
    overall rate stays interpretable; downstream callers can re-scan if
    they need exhaustive flags.
    """
    out: list[dict] = []
    for row in rows:
        gaps = (row.get("gaps_text") or "").lower()
        if not gaps:
            continue
        scores = row.get("sub_scores") or {}
        for axis, keywords in AXIS_KEYWORDS.items():
            score = scores.get(axis)
            if score is None:
                continue
            try:
                if int(score) < HIGH_SCORE_THRESHOLD:
                    continue
            except (TypeError, ValueError):
                continue
            if any(kw in gaps for kw in keywords):
                out.append(
                    {
                        "axis": axis,
                        "score": score,
                        "gaps_text": gaps,
                    }
                )
                break
    return out


__all__ = [
    "AXIS_KEYWORDS",
    "HIGH_SCORE_THRESHOLD",
    "apply_false_positive_rate",
    "bias",
    "bootstrap_ci",
    "classification_metrics",
    "coherence_violations",
    "confusion_matrix",
    "icc",
    "mae",
    "qw_kappa",
]
