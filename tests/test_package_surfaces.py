"""Public-surface sentinels for the 4 split packages (R2.5).

Reconciliation Plan v1 R2.5: propagate the S7b
test_pipeline_detector_public_surface pattern to scheduler/, ats_scanner/,
and careers_crawler/. (pipeline_detector/ already has its own sentinel
in tests/test_pipeline_detector_invariants.py.) The db/ package, when
extracted by Reconciliation R3, will have its own sentinel from R3.

Why this exists: the 7-series module splits each retain a stable
test-facing public surface via re-exports in each package's __init__.py.
If a future refactor drops a re-export, dozens of test cases will
ImportError with a confusing message; this single sentinel fails first
with one clear "missing re-export" message.

Pattern: enumerate every symbol that test files import from the package,
plus every attribute-name that test files patch (`patch("pkg.os.environ.get")`,
`patch("pkg.requests.get")`, etc.). The patch targets must resolve
through normal attribute lookup, which means the package's __init__.py
must `import` those modules even if no test does an explicit
`from pkg import os`.
"""

from __future__ import annotations


def test_scheduler_public_surface():
    """Names imported or attribute-patched by tests must remain on the package."""
    from job_finder.web import scheduler

    required = [
        # Test-imported symbols (tests/test_scheduler.py)
        "init_scheduler",
        "get_scheduler",
        "reset_scheduler",
        "run_sync_now",
        # Conftest patches `job_finder.web.scheduler._acquire_scheduler_pidfile`
        "_acquire_scheduler_pidfile",
        # Test patches `job_finder.web.scheduler.BackgroundScheduler`
        "BackgroundScheduler",
        # Test patches `job_finder.web.scheduler.os.environ.get`
        "os",
    ]
    missing = [name for name in required if not hasattr(scheduler, name)]
    assert not missing, (
        f"scheduler/ package surface missing names: {missing}. "
        "Tests will fail with ImportError or AttributeError until each is "
        "re-exported from job_finder/web/scheduler/__init__.py."
    )


def test_ats_scanner_public_surface():
    """Names imported or attribute-patched by tests must remain on the package."""
    from job_finder.web import ats_scanner

    required = [
        # Test-imported symbols (tests/test_ats_scanner.py)
        "_title_matches",
        "upsert_company",
        "derive_slug_candidates",
        "probe_ats_slugs",
        "run_ats_scan",
        # Test patches `job_finder.web.ats_scanner.requests.get`
        "requests",
    ]
    missing = [name for name in required if not hasattr(ats_scanner, name)]
    assert not missing, (
        f"ats_scanner/ package surface missing names: {missing}. "
        "Tests will fail with ImportError or AttributeError until each is "
        "re-exported from job_finder/web/ats_scanner/__init__.py."
    )


def test_careers_crawler_public_surface():
    """Names imported or attribute-patched by tests must remain on the package."""
    from job_finder.web import careers_crawler

    required = [
        # Test-imported symbols (tests/test_careers_crawler.py:12)
        "_clean_title",
        "_extract_jobs_from_soup",
        "_extract_jsonld_postings",
        "_try_static_extract",
        "crawl_careers_batch",
        # Test patches (subset that intersects test_careers_crawler.py)
        "_try_playwright_active",
        "_score_new_jobs",
        "sync_playwright",
        "requests",
    ]
    missing = [name for name in required if not hasattr(careers_crawler, name)]
    assert not missing, (
        f"careers_crawler/ package surface missing names: {missing}. "
        "Tests will fail with ImportError or AttributeError until each is "
        "re-exported from job_finder/web/careers_crawler/__init__.py."
    )
