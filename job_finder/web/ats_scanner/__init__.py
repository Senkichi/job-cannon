"""ATS (Applicant Tracking System) scanner and company registry module.

Provides:
- ATS URL extraction from job source_urls (Lever, Greenhouse, Ashby)
- Company record upsert with ATS info and normalization
- Speculative ATS slug probing with persistent cache (hit/miss/pending)
- _title_matches keyword filtering utility shared by Plans 02 and 03

Architecture:
- Thread-safe: probe_ats_slugs() creates own sqlite3 connection (same pattern
  as stale_detector.py)
- TESTING guard on probe_ats_slugs to prevent external API calls in tests
- Never re-probes cached misses; never downgrades confirmed hits to pending

ATS URL patterns (Research Pattern 2):
- Lever: jobs.lever.co/{slug}/... and api.lever.co/v0/postings/{slug}
- Greenhouse: boards.greenhouse.io/{slug}/... and boards-api.greenhouse.io/v1/boards/{slug}
- Ashby: jobs.ashbyhq.com/{slug}/... (case-sensitive slug per Research Pitfall 3)
"""

import json
import logging
import sqlite3
import time
from datetime import datetime

import requests  # noqa: F401 — re-exported for test patching of requests.get

from job_finder.db import derive_classification
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.description_formatter import strip_html_to_text

# Scoring orchestrator functions for ATS-discovered job scoring (ImportError guard).
# Uses the centralized orchestrator instead of pipeline_runner's private functions,
# breaking the bidirectional dependency (ats_scanner <-> pipeline_runner).
try:
    from job_finder.web.scoring_orchestrator import score_and_persist_job
except ImportError:
    score_and_persist_job = None  # type: ignore[assignment]

# Lazy import of HTML careers scraper (ImportError guard — Plan 03)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

# Lazy import of homepage discoverer (ImportError guard — Plan 01)
try:
    from job_finder.web.homepage_discoverer import run_homepage_discovery
except ImportError:
    run_homepage_discovery = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

from job_finder.web.ats_detection import (
    derive_slug_candidates,
    extract_ats_from_urls,
)

# Canonical scanner implementations live in ats_platforms.py.
# Re-exported here for backward compatibility with existing callers
# (careers_scraper, enrichment_tiers, run_ats_scan loop, tests/test_ats_scanner.py).
from job_finder.web.ats_platforms import (  # noqa: F401
    _title_matches,
    scan_ashby,
    scan_greenhouse,
    scan_lever,
)
from job_finder.web.ats_prober import (  # noqa: F401
    _BACKOFF_HOURS,
    _MAX_RETRIES,
    _PERMANENT_MISS_CODES,
    _PROBE_STATUS_PRECEDENCE,
    _PROBE_TIMEOUT,
    _TRANSIENT_CODES,
    _compute_retry_after,
    _handle_scan_error,
    _is_transient_error,
    _probe_ashby,
    _probe_greenhouse,
    _probe_lever,
    _probe_lever_with_result,
    _probe_smartrecruiters,
    _probe_workday,
    _reset_retry_state,
    probe_single_company,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# upsert_company lives in ats_scanner._upsert; re-exported below for the
# established `from job_finder.web.ats_scanner import upsert_company` contract.
from job_finder.web.ats_scanner._upsert import upsert_company  # noqa: E402,F401

# probe_ats_slugs lives in ats_scanner._probe; re-exported below for the
# established `from job_finder.web.ats_scanner import probe_ats_slugs` contract.
from job_finder.web.ats_scanner._probe import probe_ats_slugs  # noqa: E402,F401

# promote_ats_from_source_urls lives in ats_scanner._promote; re-exported below.
from job_finder.web.ats_scanner._promote import (  # noqa: E402,F401
    promote_ats_from_source_urls,
)

# run_ats_scan + its phase helpers live in ats_scanner._run; re-exported below.
from job_finder.web.ats_scanner._run import run_ats_scan  # noqa: E402,F401
