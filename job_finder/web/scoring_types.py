"""Shared types for the scoring pipeline.

Provides:
    JobRow -- TypedDict describing the dict shape returned by SQLite row_factory
              for job records. Used as a type hint in scoring function signatures.
    ScoringResult -- Discriminated return type that lets callers distinguish
              success from budget_exceeded, error, and skipped outcomes.
    unwrap_scoring_result -- Helper to extract the data dict from a ScoringResult,
              returning None for any non-success status.

Usage:
    from job_finder.web.scoring_types import JobRow, ScoringResult, unwrap_scoring_result

    def score(job_row: JobRow) -> ScoringResult: ...
"""

from typing import Literal, NamedTuple, Optional, TypedDict

class JobRow(TypedDict, total=False):
    """Shape of a job record dict as returned by SQLite row_factory.

    All fields are optional (total=False) because callers use .get() with
    defaults -- the DB may return NULL for any nullable column.  This exists
    purely for documentation and static-analysis hints; it does NOT enforce
    values at runtime.
    """

    dedup_key: str
    title: str
    company: str
    location: str
    description: str
    jd_full: str | None
    salary_min: int | None
    salary_max: int | None
    source_urls: str
    comp_data_json: str | None
    haiku_score: int | None
    sonnet_score: float | None
    opus_score: float | None
    fit_analysis: str | None
    enrichment_tier: str | None
    company_id: str | None
    status: str

def format_salary_range(salary_min: int | None, salary_max: int | None) -> str:
    """Format salary_min/salary_max into a human-readable range string.

    Returns:
        e.g. "$80,000 - $120,000", "$80,000+", "up to $120,000", or "Not specified".
    """
    if salary_min is not None and salary_max is not None:
        return f"${salary_min:,} - ${salary_max:,}"
    elif salary_min is not None:
        return f"${salary_min:,}+"
    elif salary_max is not None:
        return f"up to ${salary_max:,}"
    return "Not specified"

class ScoringResult(NamedTuple):
    """Discriminated return type for score_job_haiku and evaluate_job_sonnet.

    Attributes:
        data: The scoring result dict on success, None otherwise.
        status: Why scoring ended -- 'success', 'budget_exceeded', 'error',
                or 'skipped' (precondition not met, e.g. missing jd_full).
    """

    data: dict | None
    status: Literal["success", "budget_exceeded", "error", "skipped"]

def unwrap_scoring_result(scoring_result: ScoringResult) -> Optional[dict]:
    """Unwrap a ScoringResult, returning the data dict on success or None.

    Centralizes the success/failure dispatch so callers don't repeat the
    status-check pattern.  Returns scoring_result.data when status is
    'success', None otherwise.

    Args:
        scoring_result: A ScoringResult from score_job_haiku or
                        evaluate_job_sonnet.

    Returns:
        The result data dict on success, or None for any non-success status.
    """
    if scoring_result.status != "success":
        return None
    return scoring_result.data
