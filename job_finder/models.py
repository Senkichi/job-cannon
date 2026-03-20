"""Data models for Job Finder."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Job:
    """Normalized job representation across all sources."""

    title: str
    company: str
    location: str
    source: str  # "linkedin", "glassdoor", "serpapi", etc.
    source_url: str
    source_id: str = ""  # platform-specific job ID

    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    description: Optional[str] = None
    posted_date: Optional[datetime] = None
    fetched_date: datetime = field(default_factory=datetime.now)

    # Scoring (populated by scorer)
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)

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

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "source": self.source,
            "source_url": self.source_url,
            "source_id": self.source_id,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "description": self.description,
            "posted_date": self.posted_date.isoformat() if self.posted_date else None,
            "fetched_date": self.fetched_date.isoformat(),
            "score": self.score,
        }
