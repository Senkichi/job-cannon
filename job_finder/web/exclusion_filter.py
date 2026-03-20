"""Pre-Haiku exclusion filter. Zero API cost -- pure string matching.

Provides should_exclude() to determine whether a job should be skipped
before any Haiku API call, based on title keywords, excluded companies,
and a configurable salary floor.
"""

import logging
from typing import Optional

from job_finder.config import COMPANY_DENYLIST, get_company_denylist

logger = logging.getLogger(__name__)


def should_exclude(
    job_row: dict,
    exclusions: dict,
    min_salary: Optional[int] = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """Check if a job should be excluded before Haiku scoring.

    Args:
        job_row: Job record dict with at minimum: title (str), company (str),
                 salary_max (int|None).
        exclusions: Dict with optional keys:
                    - title_keywords (list[str]): Substrings to match against job title.
                    - companies (list[str]): Company names to exclude.
        min_salary: Candidate's minimum acceptable salary. If provided and salary_max
                    is disclosed and < min_salary * 0.85, the job is excluded.
                    Pass None to skip salary floor check.
        config: Optional full config dict. If provided, merges config.yaml
                filters.company_denylist entries with hardcoded defaults.
                If None, only the hardcoded COMPANY_DENYLIST is used.

    Returns:
        (True, reason_string) if the job should be excluded, (False, "") otherwise.
        Returns the first matching exclusion reason (title keywords checked first,
        then companies, then salary floor).
    """
    title = job_row.get("title", "") or ""
    company = job_row.get("company", "") or ""
    salary_max = job_row.get("salary_max")

    title_lower = title.lower()
    company_normalized = company.lower().strip()

    # 1. Title keyword exclusions (case-insensitive substring match)
    for keyword in exclusions.get("title_keywords", []):
        if not keyword:
            continue
        if keyword.lower() in title_lower:
            return True, f"Title contains excluded keyword: '{keyword}'"

    # 2. Company exclusions (config + denylist, case-insensitive, whitespace-trimmed)
    excluded_companies = [
        c.lower().strip() for c in exclusions.get("companies", []) if c
    ]
    # Merge in the denylist (hardcoded defaults + optional config entries)
    denylist = get_company_denylist(config) if config else COMPANY_DENYLIST
    excluded_companies_set = set(excluded_companies) | denylist
    if company_normalized in excluded_companies_set:
        return True, f"Excluded company: '{company.strip()}'"

    # 3. Salary floor check (only when min_salary provided and salary_max disclosed)
    if (
        min_salary is not None
        and salary_max is not None
        and isinstance(salary_max, (int, float))
        and salary_max > 0
    ):
        floor = min_salary * 0.85
        if salary_max < floor:
            return True, f"Max salary ${salary_max:,} below floor ${min_salary:,}"

    return False, ""
