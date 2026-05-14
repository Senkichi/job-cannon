"""v3.0 ordinal scoring — JobAssessment dataclass + Python-derived classification rule.

Pure rule logic. No DB side-effects. Persistence lives in `_persistence.py`,
which imports `derive_classification` from this module.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import JobAssessment` / `derive_classification` paths
continue to work.
"""

from __future__ import annotations

from dataclasses import dataclass

# Canonical sub-score key order (matches CONTEXT D-05 and the v3 scoring prompt's
# JSON schema). Used for JSON serialization stability and for derive_classification.
_SUB_SCORE_KEYS: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


@dataclass(frozen=True)
class JobAssessment:
    """Unified v3.0 scoring result. Replaces HaikuScore + SonnetScore pair.

    Per CONTEXT D-05 (Phase 34):

      sub_scores: dict[str, int] with 6 keys (title_fit, location_fit, comp_fit,
          domain_match, seniority_match, skills_match) — each 1-5 integer.
      classification: one of apply|consider|skip|reject. Typically a sentinel
          empty string at construction time; derive_classification() at persist
          time computes the authoritative value (see D-06 rule and D-07 note
          that legitimacy_note is read from the jobs row, not from the LLM).
      rationale: dict with keys strengths, gaps, talking_points,
          resume_priority_skills (each a list[str]); serialized to the reused
          fit_analysis column per D-08.
      provider: cascade-attribution string (e.g., "ollama", "anthropic") or None.
    """

    sub_scores: dict
    classification: str
    rationale: dict
    provider: str | None = None


def derive_classification(
    sub_scores: dict,
    legitimacy_note: str | None,
    enrichment_tier: str | None = None,
    jd_full_length: int = 0,
    low_signal_threshold: int = 1500,
) -> str:
    """Python-derived 5-way classification — NOT LLM-emitted (CONTEXT D-06, anti-pattern 3).

    Rule precedence (per spec D-2.5, Phase 2d sub-fix 2/4):
      1. legitimacy_note truthy            -> "reject"
      2. enrichment exhausted + short jd   -> "low_signal"
      3. any sub-score == 1                -> "reject"
      4. all sub-scores >= 3               -> "apply"
      5. all sub-scores >= 2               -> "consider"
      6. otherwise                         -> "skip"

    The low_signal branch surfaces genuinely-no-signal jobs (enrichment cascade
    exhausted AND jd_full below threshold) honestly instead of rolling them
    into apply/consider/skip via unreliable rubric outputs. The branch sits
    BEFORE the any-axis-1 reject check on purpose: a job with insufficient JD
    text cannot be confidently rejected on rubric outputs (the 1 itself may be
    a hallucination from the model scoring against an empty prompt).

    For integer 1-5 sub-scores, branch 6 ("skip") is effectively unreachable —
    any value below 2 is 1, which already triggered reject at branch 3. The
    branch remains for defense-in-depth against future sub-score domain changes
    (e.g., 0 added as a sentinel).

    Args:
        sub_scores: dict of the 6 ordinal sub-scores (1-5 integers).
        legitimacy_note: value of the jobs.legitimacy_note column; truthy means
            ingestion-time scam/exclusion detection flagged this row.
        enrichment_tier: value of jobs.enrichment_tier ('free' | 'ddg' | 'low'
            | 'serpapi' | 'mid' | 'exhausted' | None). Only 'exhausted'
            participates in the low_signal rule; other tiers are still
            re-enrichment candidates.
        jd_full_length: character length of jobs.jd_full (0 when NULL).
        low_signal_threshold: jd_full_length below this triggers low_signal
            when enrichment is exhausted. Configurable via
            scoring.low_signal_jd_chars.

    Returns:
        One of "reject", "low_signal", "apply", "consider", "skip".
    """
    if legitimacy_note:
        return "reject"
    if enrichment_tier == "exhausted" and jd_full_length < low_signal_threshold:
        return "low_signal"
    if any(v == 1 for v in sub_scores.values()):
        return "reject"
    if all(v >= 3 for v in sub_scores.values()):
        return "apply"
    if all(v >= 2 for v in sub_scores.values()):
        return "consider"
    return "skip"
