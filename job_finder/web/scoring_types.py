"""Shared types for the scoring pipeline.

Provides:
    JobRow -- TypedDict describing the dict shape returned by SQLite row_factory
              for job records. Used as a type hint in scoring function signatures.
    ScoringResult -- Discriminated return type that lets callers distinguish
              success from budget_exceeded, error, and skipped outcomes.
    unwrap_scoring_result -- Helper to extract the data dict from a ScoringResult,
              returning None for any non-success status.
    build_description_snippet -- Intelligent JD truncation with skill-keyword
              summary and requirements-section extraction. Migrated here from
              the deleted haiku_scorer.py (Plan 4 COLLAPSE-01).
    build_comp_context -- Compensation-summary string built from comp_data_json
              (Ashby/Lever ATS payloads). Migrated from the deleted haiku_scorer.py
              (Plan 4 COLLAPSE-01).

Usage:
    from job_finder.web.scoring_types import JobRow, ScoringResult, unwrap_scoring_result

    def score(job_row: JobRow) -> ScoringResult: ...
"""

import json
import re
from typing import Literal, NamedTuple, TypedDict


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
    classification: str | None
    sub_scores_json: str | None
    opus_score: float | None
    fit_analysis: str | None
    scoring_provider: str | None
    scoring_model: str | None
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


def unwrap_scoring_result(scoring_result: ScoringResult) -> dict | None:
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


# ---------------------------------------------------------------------------
# Scoring-prompt helpers (migrated from haiku_scorer.py per COLLAPSE-01)
# ---------------------------------------------------------------------------


def build_comp_context(job_row: dict) -> str | None:
    """Build a concise compensation-context string from comp_data_json.

    Extracts equity, bonus, and benefits summaries from ATS-sourced
    compensation data (stored as JSON). Returns a short summary suitable
    for inclusion in a scoring prompt, or None if no extra comp data
    is available.
    """
    comp_data_raw = job_row.get("comp_data_json")
    if not comp_data_raw:
        return None
    try:
        comp = json.loads(comp_data_raw) if isinstance(comp_data_raw, str) else comp_data_raw
    except (ValueError, TypeError):
        return None
    if not comp or not isinstance(comp, dict):
        return None

    parts: list[str] = []
    tier_summary = comp.get("compensationTierSummary")
    if tier_summary and isinstance(tier_summary, str):
        parts.append(tier_summary.strip())
    currency = comp.get("currency")
    if currency and not tier_summary:
        comp_min = comp.get("min")
        comp_max = comp.get("max")
        if comp_min and comp_max:
            parts.append(f"{currency} {comp_min:,}-{comp_max:,}")
    return "; ".join(parts) if parts else None


def build_description_snippet(
    description: str,
    profile_skills: list[str],
    max_chars: int = 2000,
) -> str:
    """Intelligent snippet builder: 1200-char head + skill summary + requirements.

    Replaces the legacy 500-char truncation. Surfaces requirements/qualifications
    sections and skill-keyword counts from the full posting without sending the
    entire JD to the model.

    DEPRECATED — do NOT wire this into the scoring path. The 2026-06-03 JD-length
    investigation found this regex *whitelists* requirements-flavored sections and
    so silently drops Responsibilities / Location / Compensation (starving
    domain_match / location_fit / comp_fit). JDs are structured too variably for a
    regex window. The scorer now sends jd_full whole (job_scorer._build_user_message);
    superfluous-content removal belongs in upstream extraction (trafilatura + ATS
    JSON — "Layer 2"), not here. Retained only until Layer 2 lands.
    """
    if not description:
        return ""

    snippet = description[:1200].strip()

    description_lower = description.lower()
    skill_matches: dict[str, int] = {}
    for skill in profile_skills:
        if not skill:
            continue
        count = description_lower.count(skill.lower())
        if count > 0:
            skill_matches[skill] = count

    if skill_matches:
        matches_str = ", ".join(
            f"{skill} ({count}x)"
            for skill, count in sorted(skill_matches.items(), key=lambda x: -x[1])
        )
        keyword_summary = f"\n\n[Skill keyword matches in full posting: {matches_str}]"
    else:
        keyword_summary = "\n\n[No candidate skill keywords found in full posting text]"

    remaining_chars = max_chars - len(snippet) - len(keyword_summary)
    if remaining_chars > 200 and len(description) > 1200:
        req_pattern = re.compile(
            r"(requirements|qualifications|what you.ll bring|what we.re looking for|"
            r"about you|your background|skills|experience required|must.have)",
            re.IGNORECASE,
        )
        match = req_pattern.search(description, 800)
        if match:
            req_start = max(0, match.start() - 20)
            req_section = description[req_start : req_start + remaining_chars].strip()
            if req_section and req_section not in snippet:
                snippet += f"\n\n[...Requirements section:]\n{req_section}"

    return (snippet + keyword_summary)[:max_chars]
