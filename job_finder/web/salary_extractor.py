"""Deterministic salary extraction from job-description text.

Pure functions. No I/O, no LLM, no shared state. Used as a fast-path
ahead of the LLM-based parse_structured_fields in data_enricher's
_apply_post_fetch_extraction. When the regex matches a plausible
range, we skip the LLM call (saves API spend + always-deterministic
result). When it doesn't, the caller falls back to the LLM.

P1.2 convergence: this module is now a thin wrapper over
``job_finder.salary_normalizer``. The regex patterns, plausibility
bounds, and K-elision logic have all been consolidated there (D-2).
The public API is preserved so m062 and _apply_post_fetch_extraction
can call it unchanged.

Plausibility constants are re-exported from salary_normalizer so any
existing importer (e.g. enrichment_tiers.parse_structured_fields)
gets the single source of truth rather than a stale copy (D-2).
"""

from __future__ import annotations

from job_finder.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL as _MAX_PLAUSIBLE_SALARY,
)
from job_finder.salary_normalizer import (
    MIN_PLAUSIBLE_ANNUAL as _MIN_PLAUSIBLE_SALARY,
)
from job_finder.salary_normalizer import (
    NormalizedSalary,
    SalaryObservation,
    normalize_observation,
    parse_salary_text,
)

# Re-export so legacy importers (e.g. enrichment_tiers.parse_structured_fields)
# continue to resolve these names from this module (D-2: single source of truth).
MIN_PLAUSIBLE_SALARY = _MIN_PLAUSIBLE_SALARY
MAX_PLAUSIBLE_SALARY = _MAX_PLAUSIBLE_SALARY

__all__ = [
    # Re-export foundation types for callers that want to use them directly.
    "MAX_PLAUSIBLE_SALARY",
    "MIN_PLAUSIBLE_SALARY",
    "NormalizedSalary",
    "SalaryObservation",
    "extract_salary_from_text",
    "normalize_observation",
    "parse_salary_text",
]


def extract_salary_from_text(text: str | None) -> tuple[int | None, int | None]:
    """Heuristic regex pass at salary extraction from JD text.

    Returns (salary_min, salary_max) as integer annual USD when a
    plausible range is found, or (None, None) otherwise. Both values
    are guaranteed populated when the result is not (None, None) —
    callers can rely on either both-present-or-neither semantics.

    P1.2: thin wrapper — delegates entirely to
    ``salary_normalizer.parse_salary_text`` (single parser, D-2) and
    ``salary_normalizer.normalize_observation`` (single normalizer, D-2).
    Hourly-cue text now salvages to an annualized value instead of
    rejecting outright (D-3 rung 1: known period → honest annualize).
    Funding numbers ("$10M - $50M") still return (None, None) because
    the normalizer's period-unknown rung 2 keeps them in-range and
    plausibility-filters them out correctly — the cents rung 3 is
    ats_structured-only.
    """
    obs = parse_salary_text(text, provenance="jd_regex")
    if obs is None:
        return None, None
    result = normalize_observation(obs)
    if result.resolution in (
        "ok",
        "salvaged_hourly",
        "salvaged_daily",
        "salvaged_weekly",
        "salvaged_monthly",
    ):
        return result.salary_min, result.salary_max
    return None, None
