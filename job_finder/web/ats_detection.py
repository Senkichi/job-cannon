"""ATS URL pattern extraction and slug candidate derivation."""

import logging
import re
from collections.abc import Callable
from typing import Any

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

# Round 6 (2026-05-27 audit B2-roadmap) — Workable / Jobvite / Paylocity /
# Rippling. URL patterns use a first-path-segment slug rather than the
# subdomain shape of the older platforms.
#
# Workable: https://apply.workable.com/{slug} (root) or
#           https://apply.workable.com/{slug}/j/{shortcode} (job detail)
_WORKABLE_HUMAN_URL = re.compile(
    r"https?://apply\.workable\.com/([^/?#]+)",
    re.IGNORECASE,
)
# Jobvite: https://jobs.jobvite.com/{slug}[/jobs[/alljobs]]
_JOBVITE_HUMAN_URL = re.compile(
    r"https?://jobs\.jobvite\.com/([^/?#]+)",
    re.IGNORECASE,
)
# Paylocity: https://recruiting.paylocity.com/recruiting/jobs/All/{guid}/...
# The "slug" is the tenant GUID. Accept both lowercase and TitleCase paths
# (audit observed both `/recruiting/jobs/...` and `/Recruiting/Jobs/...`).
_PAYLOCITY_HUMAN_URL = re.compile(
    r"https?://(?:[^/]*)recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/"
    r"([0-9a-f-]{36})",
    re.IGNORECASE,
)
# Rippling: https://ats.rippling.com/{slug}/jobs[...]
_RIPPLING_HUMAN_URL = re.compile(
    r"https?://ats\.rippling\.com/([^/?#]+)",
    re.IGNORECASE,
)

# UKG Pro Recruiting (UltiPro). Board URL packs all three slug parts:
# https://{host}/{tenant}/JobBoard/{boardGuid} (host = recruiting2.ultipro.com).
# The scanner slug is "{host}/{tenant}/{board}"; tenant case is preserved (the
# API path is case-sensitive on the customer code), host + GUID lowercased.
_ULTIPRO_URL = re.compile(
    r"https?://(recruiting\d*\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
    re.IGNORECASE,
)

# Oracle Recruiting Cloud (Fusion Candidate Experience). The board host is the
# full Fusion pod ({pod}.fa.{region}.oraclecloud.com); the CE site number lives
# in the page path (/sites/CX_1/) or the REST finder (siteNumber=CX_1). The
# scanner slug packs "{host}|{site}", defaulting the site to CX_1 when the URL
# omits it (the near-universal single-site default).
_ORACLE_CLOUD_URL = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*\.fa\.[a-z0-9-]+\.oraclecloud\.com)",
    re.IGNORECASE,
)
_ORACLE_CLOUD_SITE = re.compile(
    r"(?:/sites/|siteNumber=)([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

# iCIMS: tenant served on careers-{slug}.icims.com or jobs-{slug}.icims.com.
# Capture the bare tenant after the prefix (exactly what _probe_icims / _board_url
# wrap back into the host); require the careers-/jobs- prefix so the vendor's own
# www.icims.com marketing host can never be mistaken for a tenant board.
_ICIMS_URL = re.compile(
    r"https?://(?:careers|jobs)-([a-z0-9][a-z0-9-]*)\.icims\.com",
    re.IGNORECASE,
)

# SuccessFactors: regional TLDs (.com, .eu), slug packs host|company_id.
# Pattern matches career{N}.successfactors.{com,eu} with company= query param.
_SUCCESSFACTORS_URL = re.compile(
    r"https?://(career\d*\.successfactors\.(?:com|eu))\b.*company=([^&]+)",
    re.IGNORECASE,
)

# SAP SuccessFactors demo / SAP internal jobs (alternative domains).
_SUCCESSFACTORS_SAP_URL = re.compile(
    r"https?://(?:sapsfdemojobs\.com|jobs\.hr\.cloud\.sap\.com)",
    re.IGNORECASE,
)

# Phenom: careers/jobs subdomain patterns. The slug is the full host.
# Phenom sites typically use careers.* or jobs.* subdomains.
# We avoid www.* to prevent false positives on marketing sites (e.g., www.oracle.com/careers).
_PHENOM_URL = re.compile(
    r"https?://(?:careers|jobs)\.([a-z0-9.-]+)",
    re.IGNORECASE,
)

# ADP Workforce Now: workforcenow.adp.com with cid= UUID parameter.
# The slug is the client ID UUID. This matches Shape A (Workforce Now).
_ADP_WORKFORCENOW_URL = re.compile(
    r"https?://workforcenow\.adp\.com/.*[?&]cid=([0-9a-fA-F-]{36})",
    re.IGNORECASE,
)

# Bump alongside material changes to the regex patterns above (contract tests).
# m049-v4: + workable / jobvite / paylocity / rippling URL patterns (round 6 audit).
# m049-v5: + icims URL pattern (careers-/jobs- tenant host) (PR-A2).
# m049-v6: + oracle_cloud URL pattern (Fusion CE pod host + site number).
# m049-v7: + ultipro URL pattern (UKG Pro Recruiting host/tenant/board GUID).
# m049-v8: + successfactors URL pattern (regional TLDs + host|company_id slug).
# m049-v9: + phenom URL pattern (careers/jobs subdomain host).
# m049-v10: refine phenom URL pattern to exclude www.* (marketing sites).
# m049-v11: + adp_workforcenow URL pattern (cid= UUID).
ATS_EXTRACTOR_VERSION = "m049-v11"

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

    # Round 6 -- Workable / Jobvite / Paylocity / Rippling.
    m = _WORKABLE_HUMAN_URL.search(url)
    if m:
        return "workable", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _JOBVITE_HUMAN_URL.search(url)
    if m:
        return "jobvite", m.group(1).lower(), _SPECIFICITY_BOARD

    m = _PAYLOCITY_HUMAN_URL.search(url)
    if m:
        # Paylocity slug is a GUID, kept as-is (no .lower() to preserve casing
        # in case some tenants use uppercase characters — unlikely but cheap).
        return "paylocity", m.group(1), _SPECIFICITY_BOARD

    m = _RIPPLING_HUMAN_URL.search(url)
    if m:
        return "rippling", m.group(1).lower(), _SPECIFICITY_BOARD

    # UKG Pro Recruiting (UltiPro). Board URL carries host + tenant + GUID.
    m = _ULTIPRO_URL.search(url)
    if m:
        host = m.group(1).lower()
        tenant = m.group(2)  # case-sensitive customer code — preserve
        board = m.group(3).lower()
        return "ultipro", f"{host}/{tenant}/{board}", _SPECIFICITY_BOARD

    # Oracle Recruiting Cloud (Fusion CE). Confident, canonical ATS host — the
    # full Fusion pod hostname is unmistakable. Slug packs "{host}|{site}".
    m = _ORACLE_CLOUD_URL.search(url)
    if m:
        host = m.group(1).lower()
        sm = _ORACLE_CLOUD_SITE.search(url)
        site = sm.group(1) if sm else "CX_1"
        return "oracle_cloud", f"{host}|{site}", _SPECIFICITY_BOARD

    # iCIMS — JS-rendered board, served by the Playwright scanner. Tenant is the
    # label after the careers-/jobs- host prefix; the captured slug is exactly
    # what _probe_icims / _board_url wrap back into a host.
    m = _ICIMS_URL.search(url)
    if m:
        return "icims", m.group(1).lower(), _SPECIFICITY_BOARD

    # SuccessFactors — regional TLDs (.com, .eu), slug packs host|company_id.
    m = _SUCCESSFACTORS_URL.search(url)
    if m:
        host = m.group(1).lower()
        company_id = m.group(2)
        return "successfactors", f"{host}|{company_id}", _SPECIFICITY_BOARD

    # SAP SuccessFactors demo / SAP internal jobs (alternative domains).
    m = _SUCCESSFACTORS_SAP_URL.search(url)
    if m:
        # These domains don't expose the public feed format; return None
        # for now (TODO(followup): custom vanity domain mapping).
        return None

    # Phenom — careers/jobs/www subdomain host. Slug is the full host.
    m = _PHENOM_URL.search(url)
    if m:
        # Extract the full host from the URL
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.netloc  # Full host (e.g., "careers.conduent.com")
        return "phenom", host.lower(), _SPECIFICITY_BOARD

    # ADP Workforce Now — cid= UUID parameter. Slug is the client ID UUID.
    m = _ADP_WORKFORCENOW_URL.search(url)
    if m:
        cid = m.group(1).lower()
        return "adp", cid, _SPECIFICITY_BOARD

    return None


def probe_hit_consistent_with_careers_url(hit_platform: str, careers_url: str | None) -> bool:
    """F6 — gate a speculative-probe hit against an existing careers_url.

    The speculative probe loop derives slug candidates from `name_raw` and
    hits the first ATS API that returns non-empty postings for any candidate.
    For famous-brand short slugs that collide with a small startup on a
    small ATS, this produces a false positive (e.g. 'Shopify' → Pinpoint
    tenant `shopify.pinpointhq.com`, which is a DIFFERENT company).

    This guard rejects a hit when the company already has a `careers_url`
    that positively identifies a DIFFERENT ATS platform. It does NOT catch
    cases where `careers_url` carries no ATS signature (e.g.
    `shopify.com/careers`) — that requires fetching the page and parsing for
    embedded-widget signatures, deferred to a future F7 (wide F6).

    Returns:
        True if the hit is consistent (URL infers same platform OR carries
        no ATS signature OR is absent). False only when the URL infers a
        different platform than `hit_platform`.
    """
    if not careers_url:
        return True
    inferred = extract_ats_from_url_best(careers_url)
    if inferred is None:
        return True
    return inferred[0] == hit_platform


def _default_http_get(url: str, timeout: float) -> Any:
    """Default GET used by `careers_url_is_live`. Lazy-imports `requests` so the
    pure helpers above remain importable in environments without network deps.
    """
    import requests

    from job_finder.web._http_constants import _HEADERS
    from job_finder.web.http_fetch import fetch_with_deadline

    return fetch_with_deadline(
        url, getter=requests.get, headers=_HEADERS, timeout=timeout, allow_redirects=True
    )


def careers_url_is_live(
    url: str | None,
    *,
    timeout: float = 5.0,
    _get: Callable[[str, float], Any] | None = None,
) -> bool | None:
    """Best-effort liveness probe for a careers URL.

    F6's pure helper (above) treats `careers_url`-inferred ATS as authoritative.
    That breaks when a company has migrated ATS platforms: the old careers_url
    still parses to its old ATS, but the live probe correctly rediscovers the
    new one. This helper lets the composite gate distinguish:

    Returns:
        True  — URL responded 2xx (careers_url is current — trust it).
        False — URL responded 404 or 410 (careers_url is stale — trust the probe).
        None  — ambiguous (timeout, 5xx, 403, network error, missing URL). Caller
                should treat as "couldn't determine" and preserve the conservative
                gate behavior (do not let the probe hit override a live-looking
                careers_url).

    `_get` is injected for testability; defaults to `_default_http_get`.
    """
    if not url:
        return None
    get = _get if _get is not None else _default_http_get
    try:
        resp = get(url, timeout)
    except Exception as exc:
        logger.debug("careers_url_is_live: %s raised %s — returning None", url, exc)
        return None
    status = getattr(resp, "status_code", None)
    if status is None:
        return None
    if 200 <= status < 300:
        return True
    if status in (404, 410):
        return False
    return None


def probe_hit_consistent_or_dead_url(
    hit_platform: str,
    careers_url: str | None,
    *,
    liveness_check: Callable[[str | None], bool | None] | None = None,
) -> bool:
    """F6 augmented — gate a probe hit, override rejection when careers_url is dead.

    Composes `probe_hit_consistent_with_careers_url` with `careers_url_is_live`:

    - If the pure helper accepts (no URL, no signature, or matching platform),
      return True immediately — no network call.
    - If the pure helper rejects (URL infers a *different* platform), probe the
      URL for liveness. A 404/410 means careers_url is stale (likely the
      company migrated ATS platforms) and the live probe hit is preferred —
      return True. Live or ambiguous (timeout/5xx/403/etc.) preserves the
      rejection — return False.

    This is the call-site gate for F4-resume and the scheduler's speculative
    probe loop. The brand-collision false positive (Shopify → Pinpoint with a
    live `jobs.lever.co/shopify` URL — hypothetical) is still caught. The
    migration false rejection (NimbleAI moved Lever→Greenhouse, old Lever URL
    404s) no longer fires.

    `liveness_check` is injected for testability; defaults to
    `careers_url_is_live` (which itself performs the HTTP request).
    """
    if probe_hit_consistent_with_careers_url(hit_platform, careers_url):
        return True
    check = liveness_check if liveness_check is not None else careers_url_is_live
    # careers_url disagrees with hit; accept the hit only if URL is provably dead.
    return check(careers_url) is False


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
