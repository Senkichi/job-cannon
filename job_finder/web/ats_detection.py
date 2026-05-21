"""ATS URL pattern extraction and slug candidate derivation."""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ATS URL Regex Patterns
# Source: github.com/lever/postings-api, developers.greenhouse.io, developers.ashbyhq.com
# ---------------------------------------------------------------------------

# Lever: both jobs.lever.co and api.lever.co patterns
_LEVER_JOBS_URL = re.compile(
    r"https?://jobs\.lever\.co/([^/?#]+)",
    re.IGNORECASE,
)
_LEVER_API_URL = re.compile(
    r"https?://api\.lever\.co/v0/postings/([^/?#]+)",
    re.IGNORECASE,
)

# Greenhouse: human-facing boards.greenhouse.io / job-boards.greenhouse.io
# and API boards-api.greenhouse.io
_GREENHOUSE_BOARDS_URL = re.compile(
    r"https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)",
    re.IGNORECASE,
)
_GREENHOUSE_API_URL = re.compile(
    r"https?://boards-api\.greenhouse\.io/v1/boards/([^/?#]+)",
    re.IGNORECASE,
)

# Ashby: case-sensitive slug (Research Pitfall 3)
_ASHBY_URL = re.compile(
    r"https?://jobs\.ashbyhq\.com/([^/?#]+)",
    # NOTE: No re.IGNORECASE — Ashby slugs are case-sensitive
)

# Workday: human-facing and API URL patterns
# Human-facing: https://{sub}.myworkdayjobs.com/{board}
# API:          https://{sub}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
# Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal")
_WORKDAY_HUMAN_URL = re.compile(
    r"https?://([^/]+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)",
    re.IGNORECASE,
)
_WORKDAY_API_URL = re.compile(
    r"https?://([^/]+)\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)",
    re.IGNORECASE,
)

# SmartRecruiters: public career pages and API
_SMARTRECRUITERS_JOBS_URL = re.compile(
    r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)",
    re.IGNORECASE,
)
_SMARTRECRUITERS_API_URL = re.compile(
    r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#]+)",
    re.IGNORECASE,
)

# Stage 4 — three additional ATS platforms (Recruitee, Breezy, JazzHR).
# Patterns match both the human-facing careers domain and the canonical API
# endpoint. The careers-page form serves as the slug; the API form gives a
# higher specificity weight when both appear so reconciliation prefers it.
_RECRUITEE_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.recruitee\.com",
    re.IGNORECASE,
)
_BREEZY_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.breezy\.hr",
    re.IGNORECASE,
)
_JAZZHR_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.applytojob\.com",
    re.IGNORECASE,
)

# Stage 4 continuation — Pinpoint, Personio, BambooHR, Teamtailor.
# Same human-facing-only pattern as the first three (no separate API host).
_PINPOINT_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.pinpointhq\.com",
    re.IGNORECASE,
)
# Personio uses {slug}.jobs.personio.{de,com}. Slug is the leftmost label.
_PERSONIO_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)",
    re.IGNORECASE,
)
_BAMBOOHR_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.bamboohr\.com",
    re.IGNORECASE,
)
_TEAMTAILOR_HUMAN_URL = re.compile(
    r"https?://([a-z0-9-]+)\.teamtailor\.com",
    re.IGNORECASE,
)

# Bump alongside material changes to the regex patterns above (contract tests).
ATS_EXTRACTOR_VERSION = "m049-v3"

# Relative pattern strength within a URL: API/canonical traces win ties in reconciliation.
_SPECIFICITY_API = 10
_SPECIFICITY_BOARD = 5


def extract_ats_from_url_best(url: str) -> tuple[str, str, int] | None:
    """Pick the strongest ATS match for a single URL.

    Prefer API/boards-api/hosted integrations over human career paths so
    tied aggregate votes break toward canonical postings feeds.

    Returns:
        Tuple (platform, slug, specificity_weight) or None.
    """
    if not isinstance(url, str) or not url.strip():
        return None

    m = _LEVER_API_URL.search(url)
    if m:
        return "lever", m.group(1), _SPECIFICITY_API

    m = _GREENHOUSE_API_URL.search(url)
    if m:
        return "greenhouse", m.group(1), _SPECIFICITY_API

    m = _WORKDAY_API_URL.search(url)
    if m:
        return "workday", f"{m.group(1)}/{m.group(2)}", _SPECIFICITY_API

    m = _SMARTRECRUITERS_API_URL.search(url)
    if m:
        return "smartrecruiters", m.group(1), _SPECIFICITY_API

    m = _LEVER_JOBS_URL.search(url)
    if m:
        return "lever", m.group(1), _SPECIFICITY_BOARD

    m = _GREENHOUSE_BOARDS_URL.search(url)
    if m:
        return "greenhouse", m.group(1), _SPECIFICITY_BOARD

    m = _ASHBY_URL.search(url)
    if m:
        return "ashby", m.group(1), _SPECIFICITY_BOARD

    m = _WORKDAY_HUMAN_URL.search(url)
    if m:
        if "/wday/" not in url.lower():
            return "workday", f"{m.group(1)}/{m.group(2)}", _SPECIFICITY_BOARD

    m = _SMARTRECRUITERS_JOBS_URL.search(url)
    if m:
        return "smartrecruiters", m.group(1), _SPECIFICITY_BOARD

    # Stage 4 additions. These platforms have no separate API vs. board domain —
    # the same subdomain serves both human and API traffic — so they always
    # carry the BOARD specificity weight.
    m = _RECRUITEE_HUMAN_URL.search(url)
    if m:
        return "recruitee", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _BREEZY_HUMAN_URL.search(url)
    if m:
        return "breezy", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _JAZZHR_HUMAN_URL.search(url)
    if m:
        return "jazzhr", m.group(1).lower(), _SPECIFICITY_BOARD

    # Stage 4 continuation — Pinpoint, Personio, BambooHR, Teamtailor.
    # Same one-domain-each pattern as the prior three; BOARD specificity.
    m = _PINPOINT_HUMAN_URL.search(url)
    if m:
        return "pinpoint", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _PERSONIO_HUMAN_URL.search(url)
    if m:
        return "personio", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _BAMBOOHR_HUMAN_URL.search(url)
    if m:
        return "bamboohr", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _TEAMTAILOR_HUMAN_URL.search(url)
    if m:
        return "teamtailor", m.group(1).lower(), _SPECIFICITY_BOARD

    return None


def extract_ats_from_urls(source_urls: list[str]) -> tuple[str | None, str | None]:
    """Extract ATS platform and slug from a list of job source URLs.

    Checks each URL against Lever, Greenhouse, and Ashby patterns.
    Returns on first match. Ashby slug preserves exact URL casing
    (per Research Pitfall 3 — Ashby slugs are case-sensitive).

    Args:
        source_urls: List of URL strings from a job record's source_urls field.

    Returns:
        Tuple of (platform, slug). Platform is lever, greenhouse, ashby,
        workday, or smartrecruiters when matched. ``(None, None)`` if none.
    """
    for url in source_urls:
        hit = extract_ats_from_url_best(url)
        if hit:
            return hit[0], hit[1]
    return None, None


def aggregate_ats_candidates_from_job_bundles(
    job_bundles: list[dict],
) -> tuple[tuple[str, str] | None, str | None]:
    """Choose a single (platform, slug) from per-job URL lists.

    Aggregate by distinct jobs (dedup_key), not duplicate URL rows. Prefer
    more jobs supporting the same board, then stronger URL specificity, then
    more recent ``last_seen`` as a lexical tie-break. If the top two
    candidates tie on all three dimensions, abstain (no silent guess).

    Args:
        job_bundles: Each item has ``dedup_key`` (str), ``last_seen`` (str|None),
            and ``urls`` (list[str]).

    Returns:
        ``((platform, slug), None)`` on success, or ``(None, reason)`` when
        there is no evidence or confidence is insufficient.
    """
    # (platform, slug) -> job_count, max_spec_sum (sum of per-job best spec), last_seen_max
    primary: dict[tuple[str, str], dict[str, int | str]] = {}

    for bundle in job_bundles:
        dk = bundle.get("dedup_key")
        urls = bundle.get("urls") or []
        if not dk or not urls:
            continue
        last_seen = bundle.get("last_seen") or ""

        per_job_best: dict[tuple[str, str], int] = {}
        for url in urls:
            hit = extract_ats_from_url_best(url)
            if not hit:
                continue
            plat, slug, spec = hit
            key = (plat, slug)
            prev = per_job_best.get(key, 0)
            if spec > prev:
                per_job_best[key] = spec

        for key, spec in per_job_best.items():
            bucket = primary.setdefault(
                key,
                {"job_count": 0, "spec_sum": 0, "last_seen_max": ""},
            )
            bucket["job_count"] = int(bucket["job_count"]) + 1
            bucket["spec_sum"] = int(bucket["spec_sum"]) + int(spec)
            ls = str(bucket["last_seen_max"])
            if last_seen > ls:
                bucket["last_seen_max"] = last_seen

    if not primary:
        return None, "no_ats_urls"

    ranked = sorted(
        primary.items(),
        key=lambda item: (
            item[1]["job_count"],
            item[1]["spec_sum"],
            item[1]["last_seen_max"],
        ),
        reverse=True,
    )

    winner_key, winner_stats = ranked[0]
    if len(ranked) > 1:
        _, second_stats = ranked[1]
        w = (winner_stats["job_count"], winner_stats["spec_sum"], winner_stats["last_seen_max"])
        s = (second_stats["job_count"], second_stats["spec_sum"], second_stats["last_seen_max"])
        if w == s:
            return None, "ambiguous_tie"

    return winner_key, None


def derive_slug_candidates(company_name: str) -> list[str]:
    """Generate ATS slug candidates from a company name.

    Produces hyphenated and concatenated variants after stripping common
    legal suffixes. Used by probe_ats_slugs for speculative probing.

    Examples:
        "Scale AI" -> ["scale-ai", "scaleai"]
        "Stripe, Inc." -> ["stripe"]
        "OpenAI" -> ["openai"]

    Args:
        company_name: Raw company name string.

    Returns:
        List of slug candidate strings (lowercase). At least one candidate.
    """
    # Normalize: lowercase, strip legal suffixes
    name = company_name.lower()
    # Strip common suffixes (inc, llc, corp, ltd, co, company)
    name = re.sub(
        r"[,\s]+(inc\.?|llc\.?|corp\.?|corporation\.?|ltd\.?|limited\.?|co\.?|company\.?)$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    # Hyphenated slug (primary) — replace non-alphanumeric runs with hyphens
    hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")

    # Concatenated slug (secondary) — remove all separators
    concatenated = re.sub(r"[^a-z0-9]+", "", name)

    candidates = [hyphenated]
    if concatenated and concatenated != hyphenated:
        candidates.append(concatenated)

    return candidates
