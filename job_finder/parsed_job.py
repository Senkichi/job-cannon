"""
ParsedJob and UnresolvedParsedJob — typed contracts for parser-owned job data.

Type system choice (D-01 decision):
    Plain dataclasses with __post_init__/classmethod validators are used rather
    than attrs. Rationale: attrs is not yet a project dependency; adding it
    requires touching pyproject.toml (high-conflict file under parallel
    dispatch) and uv.lock. Plain dataclasses satisfy all contract requirements
    with zero new deps. If Phase 47.02 reveals a need for attrs-specific
    features (post-construction mutation guards, __attrs_post_init__ hooks),
    revisit then.

Invariants enforced here:
    I-07  locations_structured non-empty when locations_raw non-empty → raises
          LocationShapeError
    I-08  title does not match _TITLE_LOCATION_BLEED_RE (Blue State paren
          shape) → UnresolvedParsedJob(reason="title_metadata_blob")
    I-09  title does not contain a locations_raw token after a paren-close →
          UnresolvedParsedJob(reason="title_cross_field_bleed")
    I-10  company not in configured denylist → raises DenylistedCompanyError
    I-13  jd_full either NULL or above content-density floor → UnresolvedParsedJob
          (reason="jd_full_junk") with jd_full=None; other fields preserved

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md §8
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from job_finder.config import get_company_denylist, load_config
from job_finder.normalizers import normalize_company, normalize_title
from job_finder.web.location_canonical import JobLocation

if TYPE_CHECKING:
    from job_finder.models import Job

# ---------------------------------------------------------------------------
# Typed source and scoring-provider aliases (§2 Glossary)
# ---------------------------------------------------------------------------

# 25 distinct ingestion-source labels that appear in the DB.
# Declared as a Literal so type checkers catch string typos at call sites.
SourceTag = Literal[
    # Gmail / email alert parsers
    "linkedin",
    "glassdoor",
    "ziprecruiter",
    "indeed",
    "monster",
    "greenhouse",
    # Search APIs
    "serpapi",
    "dataforseo",
    "thordata",
    # Portal scrapers
    "portal_jooble",
    "portal_adzuna",
    "wellfound",
    "builtin",
    "google_cse",
    # ATS platform scanners (lowercase, matching ats_detection.py output)
    "workday",
    "ashby",
    "lever",
    "smartrecruiters",
    "jobvite",
    "pinpoint",
    # Web crawlers
    "careers_crawl",
    "careers_page",
    # Pipeline-detector / IMAP / resume paths
    "off_platform_email",
    "imap",
    "resume",
]

# Scoring providers — cross-checked against claude_client.FREE_PROVIDERS
ScoringProvider = Literal[
    "ollama",
    "groq",
    "cerebras",
    "gemini",
    "anthropic",
    "heuristic",
    "claude_cli",
    "claude_code_cli",
    "gemini_cli",
    "local_bundled",
    "google_cse",
]

# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class LocationShapeError(ValueError):
    """I-07: locations_raw is non-empty but locations_structured is empty."""


class DenylistedCompanyError(ValueError):
    """I-10: company name appears in the configured denylist."""


# ---------------------------------------------------------------------------
# I-08 regex: title location bleed (Blue State paren-close shape)
# ---------------------------------------------------------------------------

# Matches:
#   ") CA"              — paren-close, optional space, 2-letter state code
#   ") New York, NY"    — Paren)City, ST shape
_TITLE_LOCATION_BLEED_RE = re.compile(
    r"\)\s*[A-Z]{2}\b"  # ") XX" — paren + optional ws + 2-letter state
    r"|"
    r"\)[A-Za-z ]+,\s*[A-Z]{2}\b",  # ")City, ST" — paren + city + comma + state
)

# ---------------------------------------------------------------------------
# I-13: jd_full content density gate
# ---------------------------------------------------------------------------

_MIN_JD_LENGTH: int = 200  # characters, post-strip

# Shell / auth-wall prefix patterns mirroring tg_jobs_jd_full_junk trigger.
# TODO Phase 46.03: replace with import from job_finder.db._jd_full when merged.
_JD_JUNK_PREFIXES: tuple[str, ...] = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)


def _is_jd_junk(text: str) -> bool:
    """Return True if jd_full content fails the I-13 density gate.

    Mirrors the logic in the tg_jobs_jd_full_junk DB trigger (m078) and the
    set_jd_full() helper from Phase 46.03. Two failure modes:
    - Text shorter than _MIN_JD_LENGTH after stripping whitespace.
    - Text whose first 200 chars (lowercased) start with a junk prefix.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_JD_LENGTH:
        return True
    prefix = stripped[:200].lower()
    return any(prefix.startswith(p) for p in _JD_JUNK_PREFIXES)


# ---------------------------------------------------------------------------
# I-09 helper: cross-field title/locations_raw bleed
# ---------------------------------------------------------------------------


def _has_title_cross_field_bleed(title: str, locations_raw: list[str]) -> bool:
    """Return True if title contains a locations_raw token after a paren-close.

    I-09 fires only when:
    - A paren-close character appears in the title, AND
    - At least one alphabetic token (2+ chars) from any locations_raw entry
      appears in the portion of the title after the last paren-close.

    Example:
        title="Software Engineer) San Francisco", locations_raw=["San Francisco, CA"]
        → True (token "San" and "Francisco" appear after ")")
    """
    if not locations_raw or ")" not in title:
        return False
    after_paren = title.split(")", 1)[-1].lower()
    for loc in locations_raw:
        for token in re.findall(r"[A-Za-z]{2,}", loc):
            if token.lower() in after_paren:
                return True
    return False


# ---------------------------------------------------------------------------
# SalaryRange
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SalaryRange:
    """Structured salary range with currency and billing period."""

    min: int | None
    max: int | None
    currency: str = "USD"
    period: str = "unknown"


# ---------------------------------------------------------------------------
# ParsedJob
# ---------------------------------------------------------------------------


@dataclass
class ParsedJob:
    """Typed contract for all parser-owned columns of the jobs table (§8.2.1).

    Fields map 1:1 to the "parser" category in db/column_categories.py.
    Construct via ParsedJob.from_job() to run I-07..I-13 validators.
    Direct construction bypasses validators — only do this in unit tests or
    when you've already applied the validators independently.
    """

    # ── Core identity ───────────────────────────────────────────────────────
    title: str
    company: str
    # derived from (company, title) — not caller-supplied; use from_job()
    dedup_key: str

    # ── Location (flat legacy + structured m066 columns) ────────────────────
    location: str = ""
    locations_raw: list[str] = field(default_factory=list)
    locations_structured: list[JobLocation] = field(default_factory=list)
    workplace_type: str = "UNSPECIFIED"
    primary_country_code: str | None = None

    # ── Sources ─────────────────────────────────────────────────────────────
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_urls_raw: list[str] = field(default_factory=list)
    source_id: str | None = None

    # ── Salary ──────────────────────────────────────────────────────────────
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    salary_period: str = "unknown"

    # ── Content ─────────────────────────────────────────────────────────────
    description: str | None = None
    jd_full: str | None = None
    description_reformatted: str | None = None

    # ── Metadata ────────────────────────────────────────────────────────────
    posted_date: datetime | None = None

    # ── Scoring (None at ingest; populated by scorer pipeline) ──────────────
    scoring_provider: str | None = None

    # ── Triage (empty on a clean ParsedJob) ─────────────────────────────────
    unresolved_reasons: list[str] = field(default_factory=list)

    # -----------------------------------------------------------------------

    @classmethod
    def from_job(
        cls,
        job: Job,
        *,
        source_meta: dict | None = None,
    ) -> ParsedJob | UnresolvedParsedJob:
        """Construct a ParsedJob (or UnresolvedParsedJob) from a Job instance.

        ``source_meta`` is an optional dict carrying fields that the Job model
        does not carry (e.g. structured location data, enriched jd_full):

            locations_raw: list[str]             — raw location strings
            locations_structured: list[JobLocation] — structured equivalents
            jd_full: str | None                  — enriched job description
            sources: list[str]                   — accumulated source labels
            source_urls: list[str]               — canonical source URLs
            source_urls_raw: list[str]           — forensic original URLs

        Validator routing (I-07..I-13):

            I-08 (title_metadata_blob)      → UnresolvedParsedJob, does NOT raise
            I-09 (title_cross_field_bleed)  → UnresolvedParsedJob, does NOT raise
            I-10 (denylist)                 → raises DenylistedCompanyError
            I-07 (location shape)           → raises LocationShapeError
            I-13 (jd_full junk)             → UnresolvedParsedJob with jd_full=None

        Reasons from I-08 / I-09 / I-13 accumulate in ``unresolved_reasons``.
        If I-10 or I-07 raise, no UnresolvedParsedJob is returned.

        Title cleaning (_clean_title) is intentionally deferred to Phase 48.01.
        """
        sm: dict = source_meta or {}

        locations_raw: list[str] = sm.get("locations_raw", [])
        locations_structured: list[JobLocation] = sm.get("locations_structured", [])
        jd_full: str | None = sm.get("jd_full")
        sources: list[str] = sm.get("sources", [job.source])
        source_urls: list[str] = sm.get("source_urls", [job.source_url])
        source_urls_raw: list[str] = sm.get("source_urls_raw", [job.source_url])

        unresolved_reasons: list[str] = []
        raw_title: str = job.title

        # I-08: title metadata blob (Blue State paren-close shape)
        if _TITLE_LOCATION_BLEED_RE.search(job.title):
            unresolved_reasons.append("title_metadata_blob")

        # I-09: title cross-field bleed (location token after paren-close)
        if _has_title_cross_field_bleed(job.title, locations_raw):
            if "title_cross_field_bleed" not in unresolved_reasons:
                unresolved_reasons.append("title_cross_field_bleed")

        # I-10: company denylist — raises DenylistedCompanyError
        config = load_config()
        denylist = get_company_denylist(config)
        if job.company.lower().strip() in denylist:
            raise DenylistedCompanyError(f"Company {job.company!r} is in the configured denylist")

        # I-07: location shape — raises LocationShapeError
        if locations_raw and not locations_structured:
            raise LocationShapeError(
                f"locations_raw has {len(locations_raw)} entries but "
                f"locations_structured is empty (I-07 violation)"
            )

        # I-13: jd_full content density gate
        clean_jd_full: str | None = jd_full
        if clean_jd_full is not None and _is_jd_junk(clean_jd_full):
            unresolved_reasons.append("jd_full_junk")
            clean_jd_full = None  # row still written, but jd_full cleared

        # Derive canonical dedup_key from validated company + title
        dedup_key = f"{normalize_company(job.company)}|{normalize_title(job.title)}"

        # Denormalize structured location fields from locations_structured[0]
        workplace_type = (
            locations_structured[0].workplace_type if locations_structured else "UNSPECIFIED"
        )
        primary_country_code = (
            locations_structured[0].country_code if locations_structured else None
        )

        # source_id: Job stores "" as the empty sentinel; convert to None
        source_id: str | None = job.source_id if job.source_id else None

        common_kwargs: dict = {
            "title": job.title,
            "company": job.company,
            "dedup_key": dedup_key,
            "location": job.location,
            "locations_raw": locations_raw,
            "locations_structured": locations_structured,
            "workplace_type": workplace_type,
            "primary_country_code": primary_country_code,
            "sources": sources,
            "source_urls": source_urls,
            "source_urls_raw": source_urls_raw,
            "source_id": source_id,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "description": job.description,
            "jd_full": clean_jd_full,
            "posted_date": job.posted_date,
            "unresolved_reasons": unresolved_reasons,
        }

        if unresolved_reasons:
            return UnresolvedParsedJob(raw_title=raw_title, **common_kwargs)

        return cls(**common_kwargs)


# ---------------------------------------------------------------------------
# UnresolvedParsedJob — sibling type (NOT a subclass of ParsedJob)
# ---------------------------------------------------------------------------


@dataclass
class UnresolvedParsedJob:
    """A job that failed one or more I-08 / I-09 / I-13 validators.

    NOT a subclass of ParsedJob — the union ``ParsedJob | UnresolvedParsedJob``
    is kept explicit so callers cannot accidentally treat an unresolved row as
    clean. Carries the same fields as ParsedJob, plus:

        raw_title: str       — the original pre-clean title (relevant when
                               title was the failing field, e.g. I-08 / I-09)
        unresolved_reasons: list[str]  — non-empty reason codes

    The row is still written by upsert_job (Phase 47.02) with
    ``unresolved_reasons`` persisted to the DB. It surfaces on /admin/review
    for human triage (Phase 47.06 / 47.07).
    """

    # ── Core identity ───────────────────────────────────────────────────────
    title: str
    company: str
    dedup_key: str

    # ── Location ────────────────────────────────────────────────────────────
    location: str = ""
    locations_raw: list[str] = field(default_factory=list)
    locations_structured: list[JobLocation] = field(default_factory=list)
    workplace_type: str = "UNSPECIFIED"
    primary_country_code: str | None = None

    # ── Sources ─────────────────────────────────────────────────────────────
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_urls_raw: list[str] = field(default_factory=list)
    source_id: str | None = None

    # ── Salary ──────────────────────────────────────────────────────────────
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    salary_period: str = "unknown"

    # ── Content ─────────────────────────────────────────────────────────────
    description: str | None = None
    jd_full: str | None = None
    description_reformatted: str | None = None

    # ── Metadata ────────────────────────────────────────────────────────────
    posted_date: datetime | None = None

    # ── Scoring ─────────────────────────────────────────────────────────────
    scoring_provider: str | None = None

    # ── Triage-specific fields ───────────────────────────────────────────────
    # non-empty by construction when produced by from_job()
    unresolved_reasons: list[str] = field(default_factory=list)
    raw_title: str = ""  # original pre-clean title from the parser
