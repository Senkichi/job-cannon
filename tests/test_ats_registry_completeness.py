"""Completeness + parity guards for the unified ATS platform registry.

These turn the class of bug that motivated ``ats_registry`` into CI failures:
a platform with a working scanner + probe but missing from the liveness
dispatch (iCIMS / oracle_cloud / ultipro fell into exactly this gap, failing
``_verify_live`` 89% of the time). Each invariant is exemptable ONLY via an
explicit capability flag on the spec — never a hardcoded skip-list.

The PARITY tests pin the registry's derived views to the legacy hand-maintained
literals that still live in their old modules, so the incremental consumer
migration (later PRs delete those literals) is provably behaviour-preserving.
"""

import pytest

import job_finder.web.ats_prober as ats_prober
from job_finder.web import ats_platforms, ats_reconciler, ats_registry
from job_finder.web.ats_scanner import _probe, _run_playwright

PLATFORMS = ats_registry.PLATFORMS
PROBE_PLATFORMS = sorted(n for n, s in PLATFORMS.items() if s.probe_attr is not None)


# --------------------------------------------------------------------------- #
# Completeness guards — half-wiring becomes a red build.                       #
# --------------------------------------------------------------------------- #
def test_scannable_platform_has_probe_or_explicit_exemption():
    """Every platform with a fetch transport must be liveness-verifiable, OR
    declare an explicit exemption (keyword_adapter / non_scannable)."""
    offenders = [
        n
        for n in ats_registry.SCANNABLE_PLATFORMS
        if PLATFORMS[n].probe_attr is None
        and not PLATFORMS[n].keyword_adapter
        and not PLATFORMS[n].non_scannable
    ]
    assert not offenders, (
        f"scannable platforms with no probe and no explicit exemption: {offenders}. "
        "Add a _probe_* + probe_attr, or set keyword_adapter/non_scannable."
    )


def test_every_probe_attr_resolves_and_dispatches():
    """Each spec.probe_attr names a real ats_prober function AND verify_live
    routes to it. This is the exact regression that killed iCIMS/oracle/ultipro."""
    for name in PROBE_PLATFORMS:
        attr = PLATFORMS[name].probe_attr
        assert hasattr(ats_prober, attr), f"{name}: ats_prober.{attr} does not exist"
        assert callable(getattr(ats_prober, attr)), f"{name}: ats_prober.{attr} not callable"


@pytest.mark.parametrize("name", PROBE_PLATFORMS)
def test_verify_live_dispatches_each_probe(name, monkeypatch):
    """verify_live(platform, slug) must call the platform's probe and return its
    result — for EVERY probe platform, including icims/oracle_cloud/ultipro."""
    attr = PLATFORMS[name].probe_attr
    monkeypatch.setattr(ats_prober, attr, lambda slug: True)
    assert ats_registry.verify_live(name, "any-slug") is True
    monkeypatch.setattr(ats_prober, attr, lambda slug: False)
    assert ats_registry.verify_live(name, "any-slug") is False


@pytest.mark.parametrize("name", ["icims", "oracle_cloud", "ultipro"])
def test_regression_new_platforms_are_verifiable(name, monkeypatch):
    """The motivating bug: these had scanners + probes but verify_live returned
    False for them. Guard that they now dispatch."""
    assert name in PROBE_PLATFORMS, f"{name} lost its probe_attr"
    monkeypatch.setattr(ats_prober, PLATFORMS[name].probe_attr, lambda slug: True)
    assert ats_registry.verify_live(name, "slug") is True


def test_verify_live_false_for_keyword_adapters_and_unknown():
    """Keyword adapters have no probe; unknown platforms are not in the registry.
    Both must return False (never raise)."""
    for name in ats_registry.KEYWORD_ADAPTER_PLATFORMS:
        assert ats_registry.verify_live(name, "slug") is False
    assert ats_registry.verify_live("not_a_platform", "slug") is False


def test_fetch_dispatch_coverage_matches_scanner_registries():
    """Identity guard (the #536 class): the registry's fetch views must equal the
    authoritative scanner registries — no scanner silently dropped from dispatch."""
    assert set(ats_registry.SCANNERS_BY_NAME) == set(ats_platforms.SCANNERS_BY_NAME)
    assert set(ats_registry.PLAYWRIGHT_SCANNERS) == set(_run_playwright._PLAYWRIGHT_SCANNERS)


def test_speculative_and_fp_prone_are_disjoint():
    """Replaces the runtime assert at _probe.py: a platform is never both
    speculative-safe and false-positive-prone."""
    both = [n for n, s in PLATFORMS.items() if s.speculative_safe and s.fp_prone]
    assert not both, f"platforms both speculative_safe and fp_prone: {both}"


def test_fp_prone_are_evidence_only_but_url_fastpath():
    """FP-prone platforms must be excluded from speculation yet reachable via the
    URL-evidence fast-path (the documented promotion route for them)."""
    for n, s in PLATFORMS.items():
        if s.fp_prone:
            assert not s.speculative_safe, f"{n} fp_prone must not be speculative_safe"
            assert s.url_fastpath, f"{n} fp_prone must remain url_fastpath-eligible"


def test_non_scannable_excluded_from_url_fastpath():
    """Generalized jobvite carve-out: a non-scannable stub must not be promotable
    via the fast-path (kept at 'miss' so careers_crawler owns it)."""
    for n, s in PLATFORMS.items():
        if s.non_scannable:
            assert not s.url_fastpath, f"{n} non_scannable must not be url_fastpath"


def test_keyword_adapter_shape():
    """A keyword adapter has a (requests) scanner but no slug-probe — the explicit
    capability that exempts it from the scannable-must-have-probe guard."""
    for n in ats_registry.KEYWORD_ADAPTER_PLATFORMS:
        s = PLATFORMS[n]
        assert s.probe_attr is None, f"{n} keyword_adapter must have no probe_attr"
        assert s.requests_scanner is not None, f"{n} keyword_adapter must have a scanner"


# --------------------------------------------------------------------------- #
# Parity guards — registry views reproduce the legacy literals byte-for-byte.  #
# Delete each parity test in the PR that removes its legacy literal.           #
# --------------------------------------------------------------------------- #
def test_parity_fp_prone():
    assert ats_registry.FP_PRONE_PLATFORMS == _probe._FP_PRONE_PLATFORMS


def test_parity_url_fastpath():
    assert ats_registry.URL_FASTPATH_PLATFORMS == _probe._URL_FASTPATH_PLATFORMS


def test_parity_speculative_ladder_order():
    assert [n for n, _ in ats_registry.SPECULATIVE_PROBES] == [n for n, _ in _probe._PROBES]


def test_parity_reconcilable():
    assert ats_registry.RECONCILABLE_PLATFORMS == ats_reconciler._RECONCILABLE_PLATFORMS


def test_parity_scanner_registry():
    assert set(ats_registry.SCANNERS_BY_NAME) == set(ats_platforms.SCANNERS_BY_NAME)


def test_non_scannable_derivation():
    # Derived from each spec's `non_scannable` flag — the registered stubs with no
    # public API. Was a hand-maintained frozenset in ats_platforms (now deleted).
    assert frozenset({"jobvite", "google"}) == ats_registry.NON_SCANNABLE_PLATFORMS


def test_scannable_target_platforms():
    # Promotion-target set for careers-link discovery = scannable minus the
    # non-scannable stubs. A real requests scanner and the Playwright-only iCIMS
    # are promotable; the stubs never are. Replaces _ats_link_discovery's
    # hand-rolled _TARGET_PLATFORMS.
    targets = ats_registry.SCANNABLE_TARGET_PLATFORMS
    assert targets == ats_registry.SCANNABLE_PLATFORMS - ats_registry.NON_SCANNABLE_PLATFORMS
    for p in ("greenhouse", "lever", "icims"):
        assert p in targets
    for stub in ("jobvite", "google"):
        assert stub not in targets


def test_parity_playwright_platforms():
    assert ats_registry.PLAYWRIGHT_PLATFORMS == _run_playwright.PLAYWRIGHT_PLATFORMS


def test_parity_verify_fastpath_dispatch(monkeypatch):
    """The registry SSOT and the PRODUCTION fast-path caller (_probe._verify_fastpath_live,
    invoked at the B2 promotion write) must dispatch identically for every url_fastpath
    platform, and both must gate non-fast-path platforms to False.

    This test's docstring long CLAIMED parity with ``_probe._verify_fastpath_live`` but only
    ever called ``ats_registry.verify_fastpath_live`` — so it was blind to the exact drift it
    named: ``successfactors``/``adp`` were parity-forced into ``_URL_FASTPATH_PLATFORMS`` but
    never got a branch in the old hand-maintained if/elif ladder, silently returning False and
    killing their careers-URL fast-path promotion. Now that _verify_fastpath_live delegates to
    the registry the two are equal by construction; exercising BOTH here keeps it that way —
    any re-introduced ladder that drops a fast-path platform fails this test immediately."""
    for name in ats_registry.URL_FASTPATH_PLATFORMS:
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is True, name
        assert _probe._verify_fastpath_live(name, "s") is True, name
        monkeypatch.setattr(ats_prober, attr, lambda slug: False)
        assert ats_registry.verify_fastpath_live(name, "s") is False, name
        assert _probe._verify_fastpath_live(name, "s") is False, name
    # platforms NOT in the fast-path set must gate to False even with a live probe
    for name in ("oracle_cloud", "ultipro", "icims", "jobvite"):
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is False, name
        assert _probe._verify_fastpath_live(name, "s") is False, name


def test_parity_url_detection_order():
    """The registry's URL_DETECTION_ORDER must preserve the exact resolution order
    of the legacy extract_ats_from_url_best if-ladder. A silent reorder is the failure
    mode this registry exists to prevent — this test captures the current order byte-for-byte."""
    from job_finder.web.ats_detection import extract_ats_from_url_best

    # Representative URLs that exercise each branch in the legacy if-ladder
    test_urls = [
        ("https://api.lever.co/v0/postings/abc123", "lever", "abc123", 10),
        ("https://boards-api.greenhouse.io/v1/boards/testco/jobs/123", "greenhouse", "testco", 10),
        (
            "https://testco.myworkdayjobs.com/wday/cxs/tenant/testco/jobs",
            "workday",
            "testco/testco",
            10,
        ),
        ("https://api.smartrecruiters.com/v1/companies/testco", "smartrecruiters", "testco", 10),
        ("https://jobs.lever.co/testco", "lever", "testco", 5),
        ("https://boards.greenhouse.io/testco", "greenhouse", "testco", 5),
        ("https://jobs.ashbyhq.com/TestCo", "ashby", "TestCo", 5),  # Case-sensitive
        ("https://testco.myworkdayjobs.com/en-US/testco", "workday", "testco/testco", 5),
        ("https://jobs.smartrecruiters.com/testco", "smartrecruiters", "testco", 5),
        ("https://testco.recruitee.com", "recruitee", "testco", 5),
        ("https://testco.breezy.hr", "breezy", "testco", 5),
        ("https://testco.applytojob.com", "jazzhr", "testco", 5),
        ("https://testco.pinpointhq.com", "pinpoint", "testco", 5),
        ("https://testco.jobs.personio.de", "personio", "testco", 5),
        ("https://testco.bamboohr.com", "bamboohr", "testco", 5),
        ("https://testco.teamtailor.com", "teamtailor", "testco", 5),
        ("https://apply.workable.com/testco", "workable", "testco", 5),
        ("https://jobs.jobvite.com/testco", "jobvite", "testco", 5),
        (
            "https://recruiting.paylocity.com/recruiting/jobs/All/550e8400-e29b-41d4-a716-446655440000",
            "paylocity",
            "550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        ("https://ats.rippling.com/testco", "rippling", "testco", 5),
        (
            "https://recruiting2.ultipro.com/TENANT/JobBoard/550e8400-e29b-41d4-a716-446655440000",
            "ultipro",
            "recruiting2.ultipro.com/TENANT/550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        ("https://pod.fa.us.oraclecloud.com", "oracle_cloud", "pod.fa.us.oraclecloud.com|CX_1", 5),
        ("https://careers-testco.icims.com", "icims", "testco", 5),
        (
            "https://career1.successfactors.com?company=testco",
            "successfactors",
            "career1.successfactors.com|testco",
            5,
        ),
        ("https://careers.conduent.com", "phenom", "careers.conduent.com", 5),
        (
            "https://workforcenow.adp.com/jobs?cid=550e8400-e29b-41d4-a716-446655440000",
            "adp",
            "550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        # MIXED-CASE regression guards: lever/greenhouse slugs and the workday
        # TENANT must preserve case byte-for-byte (a `.lower()` here silently
        # mis-slugs the scanner's API URL -> 404 -> silent scan loss, and drifts
        # DB slug identity). The pre-fix registry lowercased these; the legacy
        # extract_ats_from_url_best preserved them. Lowercase-only inputs (as the
        # rest of this list used) could not catch that — these mixed-case rows do.
        ("https://jobs.lever.co/NimbleAI", "lever", "NimbleAI", 5),
        ("https://api.lever.co/v0/postings/NimbleAI", "lever", "NimbleAI", 10),
        ("https://boards.greenhouse.io/MixedCo", "greenhouse", "MixedCo", 5),
        (
            "https://boards-api.greenhouse.io/v1/boards/MixedCo/jobs/1",
            "greenhouse",
            "MixedCo",
            10,
        ),
        (
            "https://MixedTen.myworkdayjobs.com/en-US/MixedBoard",
            "workday",
            "MixedTen/MixedBoard",
            5,
        ),
    ]

    for url, expected_platform, expected_slug, expected_spec in test_urls:
        result = extract_ats_from_url_best(url)
        assert result is not None, f"Legacy implementation returned None for {url}"
        platform, slug, spec = result
        assert platform == expected_platform, (
            f"URL {url}: expected platform {expected_platform}, got {platform}"
        )
        assert slug == expected_slug, f"URL {url}: expected slug {expected_slug}, got {slug}"
        assert spec == expected_spec, f"URL {url}: expected spec {expected_spec}, got {spec}"
