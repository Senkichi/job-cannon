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
    """ats_registry.verify_fastpath_live must match the legacy _probe._verify_fastpath_live
    for every fast-path platform (both gate on url_fastpath + same probe)."""
    for name in ats_registry.URL_FASTPATH_PLATFORMS:
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is True
        monkeypatch.setattr(ats_prober, attr, lambda slug: False)
        assert ats_registry.verify_fastpath_live(name, "s") is False
    # platforms NOT in the fast-path set must gate to False even with a live probe
    for name in ("oracle_cloud", "ultipro", "icims", "jobvite"):
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is False
