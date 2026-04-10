"""Mechanical rejection pattern extraction and trend analysis.

Extracts structured patterns from rejected jobs without LLM calls.
Complements the existing Opus-based rejection_analyzer.py with
zero-cost mechanical analysis.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class RejectionPattern:
    """Structured rejection analysis for a single job."""
    job_id: str
    company: str
    title: str
    haiku_score: float | None
    sonnet_score: float | None

    # Classification dimensions
    seniority: str = ""          # junior/mid/senior/staff/principal/exec
    domain: str = ""             # eng/data/ml/product/design/other
    has_salary: bool = False
    salary_meets_floor: bool = False
    location: str = ""
    rejection_stage: str = ""    # rejected/archived (where it died)
    days_in_pipeline: int = 0
    company_size: str = ""       # startup/small/mid-size/large
    ats_platform: str = ""       # lever/greenhouse/ashby/unknown


@dataclass
class PatternReport:
    """Aggregate rejection pattern analysis."""
    period_days: int
    total_rejections: int
    patterns: list[RejectionPattern] = field(default_factory=list)

    # Aggregate stats
    rejection_by_stage: dict[str, int] = field(default_factory=dict)
    rejection_by_seniority: dict[str, int] = field(default_factory=dict)
    rejection_by_domain: dict[str, int] = field(default_factory=dict)
    rejection_by_company_size: dict[str, int] = field(default_factory=dict)
    rejection_by_location: dict[str, int] = field(default_factory=dict)
    avg_days_in_pipeline: float = 0.0
    salary_floor_miss_rate: float = 0.0
    score_distribution: dict[str, int] = field(default_factory=dict)
    top_rejected_companies: list[tuple[str, int]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("patterns")  # Don't serialize individual patterns
        return d


# Seniority detection from title keywords
_SENIORITY_KEYWORDS = {
    "intern": "intern",
    "junior": "junior", "jr": "junior", "entry": "junior",
    "mid": "mid", "intermediate": "mid",
    "senior": "senior", "sr": "senior", "lead": "senior",
    "staff": "staff",
    "principal": "principal",
    "director": "exec", "vp": "exec", "head of": "exec",
    "chief": "exec", "cto": "exec", "ceo": "exec",
}

# Ordered: longer/more-specific phrases first to avoid premature matches
# (e.g., "Machine Learning Engineer" must match "ml" before "engineer" -> "eng").
_DOMAIN_KEYWORDS: list[tuple[str, str]] = [
    ("machine learning", "ml"), ("ml ", "ml"), ("ai ", "ml"), ("nlp", "ml"),
    ("data scientist", "data"), ("data analyst", "data"), ("analytics", "data"),
    ("product manager", "product"), ("product owner", "product"),
    ("designer", "design"), ("ux", "design"), ("ui", "design"),
    ("engineer", "eng"), ("developer", "eng"), ("swe", "eng"), ("backend", "eng"),
    ("frontend", "eng"), ("fullstack", "eng"), ("devops", "eng"), ("sre", "eng"),
]


def _detect_seniority(title: str) -> str:
    title_lower = title.lower()
    for keyword, level in _SENIORITY_KEYWORDS.items():
        if keyword in title_lower:
            return level
    return "unknown"


def _detect_domain(title: str) -> str:
    title_lower = title.lower()
    for keyword, domain in _DOMAIN_KEYWORDS:
        if keyword in title_lower:
            return domain
    return "other"


def extract_rejection_patterns(db_path: str, config: dict | None = None) -> PatternReport:
    """Extract structured patterns from all rejections in the last N days.

    This is a zero-LLM-cost analysis that runs mechanically.
    """
    from job_finder.web.db_helpers import standalone_connection

    cfg = (config or {}).get("rejection_patterns", {})
    period_days = cfg.get("period_days", 90)

    report = PatternReport(period_days=period_days, total_rejections=0)

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT j.dedup_key, j.company, j.title,
                   j.haiku_score, j.sonnet_score,
                   j.salary_min, j.pipeline_status,
                   j.first_seen, j.location,
                   c.company_size, c.ats_platform
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.pipeline_status IN ('rejected', 'archived')
              AND j.first_seen > datetime('now', ?)
            ORDER BY j.first_seen DESC
            """,
            (f"-{period_days} days",),
        ).fetchall()

        report.total_rejections = len(rows)
        if not rows:
            return report

        stage_counter: Counter = Counter()
        seniority_counter: Counter = Counter()
        domain_counter: Counter = Counter()
        size_counter: Counter = Counter()
        location_counter: Counter = Counter()
        company_counter: Counter = Counter()
        score_buckets: Counter = Counter()
        total_days = 0
        salary_checks = 0
        salary_misses = 0

        min_salary = (config or {}).get("profile", {}).get("min_salary", 0)

        for row in rows:
            pattern = RejectionPattern(
                job_id=row["dedup_key"],
                company=row["company"],
                title=row["title"],
                haiku_score=row["haiku_score"],
                sonnet_score=row["sonnet_score"],
                seniority=_detect_seniority(row["title"]),
                domain=_detect_domain(row["title"]),
                has_salary=row["salary_min"] is not None,
                salary_meets_floor=(
                    row["salary_min"] is not None
                    and min_salary > 0
                    and row["salary_min"] >= min_salary
                ),
                location=row["location"] or "unknown",
                rejection_stage=row["pipeline_status"],
                company_size=row["company_size"] or "unknown",
                ats_platform=row["ats_platform"] or "unknown",
            )

            # Pipeline duration estimate (first_seen to now as proxy)
            if row["first_seen"]:
                from datetime import datetime
                try:
                    first = datetime.fromisoformat(
                        row["first_seen"].replace("Z", "+00:00")
                    )
                    now = datetime.now(first.tzinfo) if first.tzinfo else datetime.now()
                    pattern.days_in_pipeline = max(0, (now - first).days)
                except (ValueError, TypeError):
                    pass

            report.patterns.append(pattern)

            # Aggregate
            stage_counter[pattern.rejection_stage] += 1
            seniority_counter[pattern.seniority] += 1
            domain_counter[pattern.domain] += 1
            size_counter[pattern.company_size] += 1
            location_counter[pattern.location] += 1
            company_counter[pattern.company] += 1
            total_days += pattern.days_in_pipeline

            if pattern.has_salary:
                salary_checks += 1
                if not pattern.salary_meets_floor:
                    salary_misses += 1

            # Score buckets
            score = pattern.sonnet_score or pattern.haiku_score or 0
            if score >= 80:
                score_buckets["80-100"] += 1
            elif score >= 60:
                score_buckets["60-79"] += 1
            elif score >= 40:
                score_buckets["40-59"] += 1
            else:
                score_buckets["0-39"] += 1

        report.rejection_by_stage = dict(stage_counter)
        report.rejection_by_seniority = dict(seniority_counter)
        report.rejection_by_domain = dict(domain_counter)
        report.rejection_by_company_size = dict(size_counter)
        report.rejection_by_location = dict(location_counter)
        report.avg_days_in_pipeline = round(total_days / len(rows), 1)
        report.salary_floor_miss_rate = (
            round(salary_misses / salary_checks, 2) if salary_checks else 0.0
        )
        report.score_distribution = dict(score_buckets)
        report.top_rejected_companies = company_counter.most_common(10)

        # Identify blockers (systematic issues)
        blockers = []
        if seniority_counter.get("junior", 0) > report.total_rejections * 0.3:
            blockers.append("Over 30% of rejections are junior-level roles -- consider raising seniority filter")
        if location_counter.get("other", 0) > report.total_rejections * 0.3:
            blockers.append("Over 30% of rejections are non-target locations -- tighten location filter")
        if report.salary_floor_miss_rate > 0.4:
            blockers.append(f"Salary floor miss rate is {report.salary_floor_miss_rate:.0%} -- many jobs below minimum")
        report.blockers = blockers

    return report


def run_rejection_pattern_analysis(db_path: str, config: dict | None = None) -> dict:
    """Scheduled entry point. Computes patterns and stores result."""
    from job_finder.web.db_helpers import standalone_connection

    report = extract_rejection_patterns(db_path, config)

    # Store the report in the DB for dashboard access
    with standalone_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rejection_pattern_reports
                (report_json, period_days, total_rejections, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (json.dumps(report.to_dict()), report.period_days, report.total_rejections),
        )
        conn.commit()

    logger.info(
        "Rejection pattern analysis: %d rejections, %d blockers identified",
        report.total_rejections,
        len(report.blockers),
    )
    return report.to_dict()
