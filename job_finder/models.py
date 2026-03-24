"""Data models for Job Finder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    """Normalized job representation across all sources."""

    title: str
    company: str
    location: str
    source: str  # "linkedin", "glassdoor", "serpapi", etc.
    source_url: str
    source_id: str = ""  # platform-specific job ID

    salary_min: int | None = None
    salary_max: int | None = None
    description: str | None = None
    posted_date: datetime | None = None
    fetched_date: datetime = field(default_factory=datetime.now)

    # Scoring (populated by scorer)
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.title.strip():
            raise ValueError("Job title cannot be empty")
        if not self.company.strip():
            raise ValueError("Job company cannot be empty")

    # Dedup key
    @property
    def dedup_key(self) -> str:
        """Normalized key for deduplication.

        Uses company+title only (location intentionally excluded per user decision:
        same company + same title = same job regardless of location differences).
        Normalizes company suffixes (Inc., LLC) and title abbreviations (Sr.->Senior).
        """
        from job_finder.web.dedup_normalizer import normalized_dedup_key
        return normalized_dedup_key(self.company, self.title)

