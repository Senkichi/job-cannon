"""Single source of truth for ATS platform capabilities.

Historically, "which platforms can do X" was re-enumerated by hand in ~12
places (the ``_verify_live`` if-ladder, ``_verify_fastpath_live``, ``_PROBES``,
``_FP_PRONE_PLATFORMS``, ``_URL_FASTPATH_PLATFORMS``, ``_RECONCILABLE_PLATFORMS``,
``_PLAYWRIGHT_SCANNERS``, ``NON_SCANNABLE_PLATFORMS``, posting-id patterns, ...).
Adding a platform meant editing all of them, and missing one silently degraded
behaviour with no error — which is exactly how iCIMS / oracle_cloud / ultipro
ended up with working scanners + probes but a ``_verify_live`` that returned
``False`` for them, failing promotion 89% of the time.

This module collapses those facets into ONE :class:`PlatformSpec` per platform.
Every scattered list becomes a comprehension over :data:`PLATFORMS`, and
``tests/test_ats_registry_completeness.py`` turns any future half-wiring into a
CI failure (a scannable platform with no probe, a scanner missing from dispatch,
etc.), exemptable only via an explicit capability flag — never a hardcoded skip.

Import layering (acyclic): this module sits ABOVE the leaves it imports
(``ats_platforms``, ``ats_prober``) and BELOW its consumers
(``ats_identity_reconcile``, ``ats_scanner/_probe``, ``ats_reconciler``, ...).
No leaf imports ``ats_registry``.

Probe dispatch resolves the probe function by NAME on the ``ats_prober`` module
at CALL time (``getattr(ats_prober, spec.probe_attr)``) rather than capturing a
reference at import. This preserves the documented test-patch semantics: a test
that monkeypatches ``ats_prober._probe_lever`` still takes effect.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

import job_finder.web.ats_prober as _prober
from job_finder.web.ats_platforms import PLAYWRIGHT_SCANNERS as _PLAYWRIGHT_SCANNERS
from job_finder.web.ats_platforms import SCANNERS_BY_NAME as _REQUESTS_SCANNERS
from job_finder.web.ats_platforms._platforms_icims import SCANNER as _ICIMS_SCANNER
from job_finder.web.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from job_finder.web.ats_platforms._platforms_phenom import SCANNER as _PHENOM_SCANNER
from job_finder.web.ats_platforms._registry import PlatformScanner

# ---------------------------------------------------------------------------
# URL detection patterns (migrated from ats_detection.py)
# ---------------------------------------------------------------------------
_SPECIFICITY_API = 10
_SPECIFICITY_BOARD = 5


def _extract_slug_default(match: re.Match, url: str) -> str:
    """Default slug extractor: return the first capture group, lowercased."""
    return match.group(1).lower()


def _extract_slug_preserve_case(match: re.Match, url: str) -> str:
    """Slug extractor that preserves case (for platforms where case matters)."""
    return match.group(1)


def _extract_slug_ashby(match: re.Match, url: str) -> str:
    """Ashby slug: case-sensitive (no lowercasing)."""
    return match.group(1)  # Preserve case


def _extract_slug_workday_api(match: re.Match, url: str) -> str:
    """Workday API slug: {subdomain}/{board} (middle tenant ignored)."""
    return f"{match.group(1).lower()}/{match.group(2)}"  # Board case preserved


def _extract_slug_workday_human(match: re.Match, url: str) -> str | None:
    """Workday human slug: {subdomain}/{board}. Skip if URL has /wday/ (API handles those)."""
    if "/wday/" in url.lower():
        return None  # Signal to skip - API pattern should have matched
    return f"{match.group(1).lower()}/{match.group(2)}"  # Board case preserved


def _extract_slug_ultipro(match: re.Match, url: str) -> str:
    """UltiPro slug: {host}/{tenant}/{board} (tenant case-sensitive)"""
    host = match.group(1).lower()
    tenant = match.group(2)  # case-sensitive
    board = match.group(3).lower()
    return f"{host}/{tenant}/{board}"


def _extract_slug_oracle_cloud(match: re.Match, url: str) -> str:
    """Oracle Cloud slug: {host}|{site} (default CX_1)"""
    host = match.group(1).lower()
    site_match = re.search(r"(?:/sites/|siteNumber=)([A-Za-z0-9_]+)", url, re.IGNORECASE)
    site = site_match.group(1) if site_match else "CX_1"
    return f"{host}|{site}"


def _extract_slug_phenom(match: re.Match, url: str) -> str:
    """Phenom slug: full host from URL"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc.lower()


def _extract_slug_successfactors(match: re.Match, url: str) -> str:
    """SuccessFactors slug: {host}|{company_id}"""
    host = match.group(1).lower()
    company_id = match.group(2)
    return f"{host}|{company_id}"


def _extract_slug_paylocity(match: re.Match, url: str) -> str:
    """Paylocity slug: GUID (preserve case)"""
    return match.group(1)  # GUID, keep as-is


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's cross-facet capabilities. The dict key in :data:`PLATFORMS`.

    Exactly one fetch transport is set for a scannable platform
    (``requests_scanner`` xor ``playwright_scanner``); keyword adapters set
    neither slug-probe nor URL form and declare ``keyword_adapter=True``.
    """

    name: str
    # FETCH (attached from the scanner registries below)
    requests_scanner: PlatformScanner | None = None
    playwright_scanner: PlaywrightPlatformScanner | None = None
    # LIVENESS — attribute name on ats_prober, resolved via getattr at call time.
    probe_attr: str | None = None
    # CAPABILITY FLAGS (each scattered list/ladder derives from one of these)
    fp_prone: bool = False
    speculative_safe: bool = False
    speculative_order: int | None = None
    url_fastpath: bool = False
    reconcilable: bool = False
    non_scannable: bool = False
    keyword_adapter: bool = False


# --- The registry. ONE entry per platform; capability flags only here. ---------
# Fetch-scanner objects are attached from the scanner registries afterwards so
# this table stays readable and cannot drift from SCANNERS_BY_NAME.
_SPECS: tuple[PlatformSpec, ...] = (
    # Speculative-ladder platforms (order is load-bearing: fastest JSON first).
    PlatformSpec(
        "lever",
        probe_attr="_probe_lever",
        speculative_safe=True,
        speculative_order=0,
        url_fastpath=True,
        reconcilable=True,
    ),
    PlatformSpec(
        "greenhouse",
        probe_attr="_probe_greenhouse",
        speculative_safe=True,
        speculative_order=1,
        url_fastpath=True,
        reconcilable=True,
    ),
    PlatformSpec(
        "ashby",
        probe_attr="_probe_ashby",
        speculative_safe=True,
        speculative_order=2,
        url_fastpath=True,
        reconcilable=True,
    ),
    PlatformSpec(
        "jazzhr",
        probe_attr="_probe_jazzhr",
        speculative_safe=True,
        speculative_order=3,
        url_fastpath=True,
    ),
    PlatformSpec(
        "pinpoint",
        probe_attr="_probe_pinpoint",
        speculative_safe=True,
        speculative_order=4,
        url_fastpath=True,
    ),
    PlatformSpec(
        "teamtailor",
        probe_attr="_probe_teamtailor",
        speculative_safe=True,
        speculative_order=5,
        url_fastpath=True,
    ),
    # Reconcile-only enterprise boards (POST APIs; not speculative-probed).
    PlatformSpec("workday", probe_attr="_probe_workday", url_fastpath=True, reconcilable=True),
    PlatformSpec(
        "smartrecruiters",
        probe_attr="_probe_smartrecruiters",
        url_fastpath=True,
        reconcilable=True,
    ),
    # FP-prone: evidence/URL-path promotable only (never speculative-guessed).
    PlatformSpec("bamboohr", probe_attr="_probe_bamboohr", fp_prone=True, url_fastpath=True),
    PlatformSpec("personio", probe_attr="_probe_personio", fp_prone=True, url_fastpath=True),
    PlatformSpec("recruitee", probe_attr="_probe_recruitee", fp_prone=True, url_fastpath=True),
    PlatformSpec("breezy", probe_attr="_probe_breezy", fp_prone=True, url_fastpath=True),
    # Round-6 URL-fastpath additions.
    PlatformSpec("workable", probe_attr="_probe_workable", url_fastpath=True),
    PlatformSpec("paylocity", probe_attr="_probe_paylocity", url_fastpath=True),
    PlatformSpec("rippling", probe_attr="_probe_rippling", url_fastpath=True),
    # Probe exists but reconcile-only (not in the speculative fast-path today).
    PlatformSpec("oracle_cloud", probe_attr="_probe_oracle_cloud"),
    PlatformSpec("ultipro", probe_attr="_probe_ultipro"),
    PlatformSpec("ibm", probe_attr="_probe_ibm"),
    # SuccessFactors — public XML feed, URL-fastpath eligible.
    PlatformSpec(
        "successfactors", probe_attr="_probe_successfactors", url_fastpath=True, reconcilable=True
    ),
    # ADP Workforce Now — public JSON feed, URL-fastpath eligible.
    PlatformSpec("adp", probe_attr="_probe_adp", url_fastpath=True, reconcilable=True),
    # Playwright-fetch (no requests API); promotable via reconcile.
    PlatformSpec("icims", playwright_scanner=_ICIMS_SCANNER, probe_attr="_probe_icims"),
    # Phenom — Playwright scanner via sitemap, no public JSON API.
    PlatformSpec("phenom", playwright_scanner=_PHENOM_SCANNER, probe_attr="_probe_phenom"),
    # Registered stub with a probe but kept at 'miss' (careers_crawler owns it).
    PlatformSpec("jobvite", probe_attr="_probe_jobvite", non_scannable=True),
    # Keyword-search adapters: scanner but no slug-probe and no URL form. The
    # explicit capability that exempts them from the scannable-must-have-probe
    # guard (never a hardcoded skip-list).
    PlatformSpec("amazon", keyword_adapter=True),
    PlatformSpec("microsoft", keyword_adapter=True),
    PlatformSpec("eightfold", keyword_adapter=True),
    # Registered stub, no public API (returns []).
    PlatformSpec("google", non_scannable=True),
)


def _attach_scanners(specs: tuple[PlatformSpec, ...]) -> dict[str, PlatformSpec]:
    """Bind each spec to its requests-scanner from SCANNERS_BY_NAME (the owner of
    the scanner objects). iCIMS and Phenom have no requests scanner (playwright only)."""
    out: dict[str, PlatformSpec] = {}
    for spec in specs:
        rs = _REQUESTS_SCANNERS.get(spec.name)
        ps = _PLAYWRIGHT_SCANNERS.get(spec.name)
        if rs is not None:
            out[spec.name] = replace(spec, requests_scanner=rs)
        elif ps is not None:
            out[spec.name] = replace(spec, playwright_scanner=ps)
        else:
            out[spec.name] = spec
    return out


PLATFORMS: dict[str, PlatformSpec] = _attach_scanners(_SPECS)


# --- Liveness dispatch (call-time getattr preserves monkeypatch semantics) -----
def _resolve_probe(probe_attr: str):
    return getattr(_prober, probe_attr)


def verify_live(platform: str, slug: str) -> bool:
    """True if ``slug`` resolves to a live board on ``platform``.

    Table lookup into the registry, replacing the former hand-maintained
    if-ladder in ``ats_identity_reconcile``. Returns False for unknown platforms
    or platforms with no probe (keyword adapters / pure stubs)."""
    spec = PLATFORMS.get(platform)
    if spec is None or spec.probe_attr is None:
        return False
    return bool(_resolve_probe(spec.probe_attr)(slug))


def verify_fastpath_live(platform: str, slug: str) -> bool:
    """Liveness gate for the speculative prober's B2 URL-evidence fast-path.

    Same dispatch as :func:`verify_live` but gated on ``url_fastpath`` so only
    the audited fast-path set is verifiable here."""
    spec = PLATFORMS.get(platform)
    if spec is None or not spec.url_fastpath or spec.probe_attr is None:
        return False
    return bool(_resolve_probe(spec.probe_attr)(slug))


# --- Derived views (single source for every formerly-hand-maintained list) -----
SCANNERS_BY_NAME: dict[str, PlatformScanner] = {
    n: s.requests_scanner for n, s in PLATFORMS.items() if s.requests_scanner is not None
}
PLAYWRIGHT_SCANNERS: dict[str, PlaywrightPlatformScanner] = {
    n: s.playwright_scanner for n, s in PLATFORMS.items() if s.playwright_scanner is not None
}
PLAYWRIGHT_PLATFORMS: frozenset[str] = frozenset(PLAYWRIGHT_SCANNERS)
NON_SCANNABLE_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.non_scannable
)
FP_PRONE_PLATFORMS: frozenset[str] = frozenset(n for n, s in PLATFORMS.items() if s.fp_prone)
URL_FASTPATH_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.url_fastpath
)
RECONCILABLE_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.reconcilable
)
KEYWORD_ADAPTER_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.keyword_adapter
)

# URL detection ordering: load-bearing flat list of (platform, pattern, specificity, extractor)
# for extract_ats_from_url_best. Replaces the hand-maintained if-ladder in ats_detection.py.
# The order is byte-for-byte preserved by the parity test.
_URL_DETECTION_PATTERNS: list[
    tuple[str, re.Pattern, int, Callable[[re.Match, str], str | None]]
] = [
    # Order 0: Lever API
    (
        "lever",
        re.compile(r"https?://api\.lever\.co/v0/postings/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_default,
    ),
    # Order 1: Greenhouse API
    (
        "greenhouse",
        re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_default,
    ),
    # Order 2: Workday API
    (
        "workday",
        re.compile(
            r"https?://([^/]+)\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)", re.IGNORECASE
        ),
        _SPECIFICITY_API,
        _extract_slug_workday_api,
    ),
    # Order 3: SmartRecruiters API
    (
        "smartrecruiters",
        re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_preserve_case,
    ),
    # Order 4: Lever board
    (
        "lever",
        re.compile(r"https?://jobs\.lever\.co/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 5: Greenhouse board
    (
        "greenhouse",
        re.compile(r"https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 6: Ashby (case-sensitive)
    (
        "ashby",
        re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)"),
        _SPECIFICITY_BOARD,
        _extract_slug_ashby,
    ),
    # Order 7: Workday human (with /wday/ skip)
    (
        "workday",
        re.compile(r"https?://([^/]+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_workday_human,
    ),
    # Order 8: SmartRecruiters board
    (
        "smartrecruiters",
        re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_preserve_case,
    ),
    # Order 9: Recruitee
    (
        "recruitee",
        re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 10: Breezy
    (
        "breezy",
        re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 11: JazzHR
    (
        "jazzhr",
        re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 12: Pinpoint
    (
        "pinpoint",
        re.compile(r"https?://([a-z0-9-]+)\.pinpointhq\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 13: Personio
    (
        "personio",
        re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 14: BambooHR
    (
        "bamboohr",
        re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 15: Teamtailor
    (
        "teamtailor",
        re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 16: Workable
    (
        "workable",
        re.compile(r"https?://apply\.workable\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 17: Jobvite
    (
        "jobvite",
        re.compile(r"https?://jobs\.jobvite\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 18: Paylocity
    (
        "paylocity",
        re.compile(
            r"https?://(?:[^/]*)recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-f-]{36})",
            re.IGNORECASE,
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_paylocity,
    ),
    # Order 19: Rippling
    (
        "rippling",
        re.compile(r"https?://ats\.rippling\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 20: UltiPro
    (
        "ultipro",
        re.compile(
            r"https?://(recruiting\d*\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
            re.IGNORECASE,
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_ultipro,
    ),
    # Order 21: Oracle Cloud
    (
        "oracle_cloud",
        re.compile(
            r"https?://([a-z0-9][a-z0-9-]*\.fa\.[a-z0-9-]+\.oraclecloud\.com)", re.IGNORECASE
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_oracle_cloud,
    ),
    # Order 22: iCIMS
    (
        "icims",
        re.compile(r"https?://(?:careers|jobs)-([a-z0-9][a-z0-9-]*)\.icims\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 23: SAP SuccessFactors demo (returns None) - must be before Phenom
    (
        "successfactors",
        re.compile(r"https?://(?:sapsfdemojobs\.com|jobs\.hr\.cloud\.sap\.com)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        lambda m, u: None,
    ),
    # Order 24: Phenom
    (
        "phenom",
        re.compile(r"https?://(?:careers|jobs)\.([a-z0-9.-]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_phenom,
    ),
    # Order 25: SuccessFactors
    (
        "successfactors",
        re.compile(
            r"https?://(career\d*\.successfactors\.(?:com|eu))\b.*company=([^&]+)", re.IGNORECASE
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_successfactors,
    ),
    # Order 26: ADP
    (
        "adp",
        re.compile(r"https?://workforcenow\.adp\.com/.*[?&]cid=([0-9a-fA-F-]{36})", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
]

# Speculative ladder: ordered (platform, probe_fn) pairs, fastest first. Probe
# refs captured here (import-time) match the prior _PROBES behaviour exactly.
SPECULATIVE_PROBES: list[tuple[str, object]] = [
    (s.name, _resolve_probe(s.probe_attr))
    for s in sorted(
        (s for s in PLATFORMS.values() if s.speculative_safe and s.probe_attr is not None),
        key=lambda s: s.speculative_order if s.speculative_order is not None else 1_000,
    )
]

# The scannable population the completeness guard reasons over: anything with a
# fetch transport (requests or playwright).
SCANNABLE_PLATFORMS: frozenset[str] = frozenset(
    n
    for n, s in PLATFORMS.items()
    if s.requests_scanner is not None or s.playwright_scanner is not None
)

# Promotion-target population for careers-link discovery: every scannable
# platform except the non-scannable stubs (jobvite/google). Replaces
# _ats_link_discovery's hand-rolled
# ``_TARGET_PLATFORMS = (SCANNERS_BY_NAME - NON_SCANNABLE) | {icims}`` — a
# careers link to a platform in this set is promotable; one to a stub is not.
SCANNABLE_TARGET_PLATFORMS: frozenset[str] = SCANNABLE_PLATFORMS - NON_SCANNABLE_PLATFORMS
