"""Source-fetch and persistence helpers for the ingestion pipeline.

All private helpers used by run_ingestion live here. They are re-exported
from pipeline_runner so existing patch paths (job_finder.web.pipeline_runner.*)
continue to work without changes.
"""

import logging
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from job_finder.sources.dataforseo_source import DataForSEOSource

from job_finder.config import DEFAULT_LOOKBACK_DAYS
from job_finder.db import upsert_job
from job_finder.json_utils import utc_now_iso
from job_finder.models import Job
from job_finder.scoring.scorer import JobScorer
from job_finder.secrets import get_secret

try:
    from job_finder.sources.gmail_source import GmailSource
except ImportError:
    GmailSource = None  # type: ignore[assignment,misc]

try:
    from job_finder.sources.imap_source import ImapSource
except ImportError:
    ImapSource = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _fetch_gmail(config: dict, conn: sqlite3.Connection, summary: dict) -> list[Job]:
    """Fetch jobs from Gmail with per-message deduplication via email_parse_log.

    Before fetching, queries email_parse_log for message IDs already processed
    within the lookback window and passes them to GmailSource.fetch_jobs() so
    those messages are skipped entirely. After fetching, bulk-inserts the newly
    processed IDs so they are skipped on the next sync.

    Args:
        config: Full config dict.
        conn: SQLite connection for email_parse_log writes.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects parsed from Gmail.
    """
    gmail_config = config.get("sources", {}).get("gmail", {})
    if not gmail_config.get("enabled", True):
        logger.debug("Gmail source disabled in config.")
        return []

    run_id = f"gmail_run_{datetime.now().isoformat()}"
    lookback_days = gmail_config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)

    # --- Query known message IDs from email_parse_log ---
    known_ids: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT message_id FROM email_parse_log"
            " WHERE sender = 'gmail'"
            " AND processed_at >= datetime('now', ?)"
            " AND message_id NOT LIKE 'gmail_run_%'",
            (f"-{lookback_days} days",),
        ).fetchall()
        known_ids = {row[0] for row in rows}
        logger.debug("Gmail dedup: %d known message IDs in email_parse_log", len(known_ids))
    except Exception as e:
        logger.warning("Failed to query email_parse_log for dedup (proceeding without): %s", e)

    if GmailSource is None:
        logger.warning("GmailSource import failed; skipping Gmail fetch")
        return []
    try:
        source = GmailSource()
        jobs, new_ids = source.fetch_jobs(
            lookback_days=lookback_days,
            processed_message_ids=known_ids,
        )
        summary["gmail_fetched"] = len(jobs)

        logger.info(
            "Gmail dedup: %d known, %d newly processed",
            len(known_ids),
            len(new_ids),
        )

        # --- Bulk-insert newly processed message IDs into email_parse_log ---
        # jobs_found=0 is a dedup-only placeholder for job-alert rows.  It does
        # NOT mean "this email had zero jobs" — it simply marks the message as
        # seen so the next sync can skip it.  This value must never be used for
        # analytics; use the jobs table directly for per-source job counts.
        if new_ids:
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO email_parse_log"
                    " (message_id, sender, processed_at, jobs_found)"
                    " VALUES (?, 'gmail', datetime('now'), 0)",
                    [(mid,) for mid in new_ids],
                )
                conn.commit()
                logger.debug(
                    "Gmail dedup: inserted %d message IDs into email_parse_log", len(new_ids)
                )
            except Exception as e:
                logger.warning("Failed to bulk-insert message IDs into email_parse_log: %s", e)

        # --- Log parse failure activity feed entries ---
        # (per locked decision: "Non-meta emails that parse to zero jobs create
        # an activity feed entry")
        # Duplicate-failure protection is implicit: messages already in
        # email_parse_log are filtered out by the dedup query above, so
        # parse_failures only contains newly processed messages.
        for failure in getattr(source, "parse_failures", []):
            try:
                fail_sender = failure.get("sender", "unknown")
                domain = (
                    fail_sender.split("@")[-1].replace(".", "_")
                    if "@" in fail_sender
                    else fail_sender
                )
                conn.execute(
                    "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)"
                    " VALUES (?, ?, 0, 0, 0)",
                    (datetime.now().isoformat(), f"{domain}_parse_failure"),
                )
                conn.commit()
                logger.debug("Zero-job email routed to activity feed: %s", fail_sender)
            except Exception as e:
                logger.warning("Failed to log parse failure to runs: %s", e)

        # --- Log the run-level summary to email_parse_log ---
        _log_to_email_parse_log(conn, run_id, "gmail", len(jobs), None)

        logger.info("Gmail: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["gmail_errors"].append(error_msg)
        logger.warning("Gmail ingestion failed: %s", error_msg)

        # Log the failure
        _log_to_email_parse_log(conn, run_id, "gmail", 0, error_msg)

        return []


def _fetch_imap(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from IMAP with server-flag deduplication.

    IMAP uses the \\Seen server flag for dedup - no database tracking required.
    This is the stranger-friendly default (app password, no OAuth).

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects parsed from IMAP.
    """
    imap_config = config.get("sources", {}).get("imap", {})
    if not imap_config.get("enabled", False):
        logger.debug("IMAP source disabled in config.")
        return []

    # Require non-empty credentials
    email = imap_config.get("email", "")
    app_password = get_secret("sources.imap.app_password", config=config) or ""
    if not email or not app_password:
        logger.warning("IMAP source enabled but credentials missing")
        summary["imap_errors"].append("IMAP credentials missing")
        return []

    host = imap_config.get("host", "imap.gmail.com")
    port = imap_config.get("port", 993)
    folder = imap_config.get("folder", "INBOX")

    _ImapSource = ImapSource  # bind to local so Pyright narrows after the None check
    if _ImapSource is None:
        logger.warning("ImapSource import failed; skipping IMAP fetch")
        return []
    try:
        source = _ImapSource(
            host=host,
            port=port,
            email_address=email,
            app_password=app_password,
            folder=folder,
        )
        jobs, _ = source.fetch_jobs()
        summary["imap_fetched"] = len(jobs)

        logger.info("IMAP: fetched %d jobs", len(jobs))
        return jobs
    except Exception as e:
        error_msg = str(e)
        summary["imap_errors"].append(error_msg)
        logger.warning("IMAP ingestion failed: %s", error_msg)
        return []


def _fetch_serpapi(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from SerpAPI with error isolation.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from SerpAPI.
    """
    serpapi_config = config.get("sources", {}).get("serpapi", {})
    if not serpapi_config.get("enabled", False):
        logger.debug("SerpAPI source disabled in config.")
        return []

    api_key = get_secret("sources.serpapi.api_key", config=config) or ""
    if not api_key:
        msg = "SerpAPI key not configured"
        summary["serpapi_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = serpapi_config.get("queries", [])
    if not queries:
        logger.debug("No SerpAPI queries configured.")
        return []

    try:
        from job_finder.sources.serpapi_source import SerpAPISource

        max_pages = serpapi_config.get("max_pages", 5)
        source = SerpAPISource(api_key, max_pages=max_pages)
        jobs = source.fetch_jobs(queries)
        summary["serpapi_fetched"] = len(jobs)

        logger.info("SerpAPI: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["serpapi_errors"].append(error_msg)
        logger.warning("SerpAPI ingestion failed: %s", error_msg)
        return []


def _fetch_thordata(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from Thordata Google Jobs SERP API with error isolation.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from Thordata.
    """
    thordata_config = config.get("sources", {}).get("thordata", {})
    if not thordata_config.get("enabled", False):
        logger.debug("Thordata source disabled in config.")
        return []

    api_key = get_secret("sources.thordata.api_key", config=config) or ""
    if not api_key:
        msg = "Thordata API key not configured"
        summary["thordata_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = thordata_config.get("queries", [])
    if not queries:
        logger.debug("No Thordata queries configured.")
        return []

    max_age_days = thordata_config.get("max_age_days", 3)

    try:
        from job_finder.sources.thordata_source import ThordataSource

        source = ThordataSource(api_key, max_age_days=max_age_days)
        jobs = source.fetch_jobs(queries)
        summary["thordata_fetched"] = len(jobs)

        logger.info("Thordata: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["thordata_errors"].append(error_msg)
        logger.warning("Thordata ingestion failed: %s", error_msg)
        return []


def _fetch_portal_search(
    config: dict,
    summary: dict,
    *,
    include_cse: bool = True,
    db_path: str | None = None,
) -> list[Job]:
    """Fetch from niche job portals: free APIs first, SERP fallback.

    Tiers (executed in order inside ``fetch_all_portals``):
      1a. Always-on free API portals (RemoteOK, Remotive, Himalayas) — zero cost.
      1b. Stage-2 free portals (Jobicy, YC, USAJobs, Adzuna, Jooble) — gated by
          ``sources.portal_search.<name>.enabled``. Keyless or free-with-reg.
      2.  SERP portals (``site:`` queries) — DataForSEO preferred when keyed,
          Google CSE used as the free fallback when only CSE is configured.
          See PLAN.md load-bearing decision #8: CSE once/day, hence the
          ``include_cse`` gate (caller's job to decide which run gets it).

    SerpAPI and Thordata are NOT used for portal_search — too expensive for
    ``site:`` queries.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.
        include_cse: When False, suppresses Google CSE backend construction
            even if it's enabled in config. Used by the scheduler to keep CSE
            on a once-per-day cadence (one of the 3x/day ingestion slots) while
            free-API portals run on every slot. Manual sync paths leave the
            default True so user-initiated runs include CSE if configured.

    Returns:
        List of Job objects from portal searches.
    """
    portal_cfg = config.get("sources", {}).get("portal_search", {})
    if not portal_cfg.get("enabled", False):
        return []

    # Stage 7.4 (Finding #3): fall back to profile.target_titles when
    # portal_search.keywords is empty. Matches the benchmark behavior
    # (scripts/benchmark_sources.py::_portal_keywords) so a user who toggles
    # the Stage 7 master switch without manually populating keywords still
    # gets meaningful queries. Settings UI hints at this with helper copy
    # under the keywords textarea.
    keywords = portal_cfg.get("keywords") or []
    used_fallback = False
    if not keywords:
        keywords = list(config.get("profile", {}).get("target_titles") or [])
        if not keywords:
            logger.info(
                "Portal search: no keywords and no profile.target_titles, skipping"
            )
            return []
        used_fallback = True
        logger.info(
            "Portal search: keywords empty, falling back to %d target_titles",
            len(keywords),
        )
    # Stage 7.9 observability: surface whether the 7.4 fallback path fired so
    # the dashboard activity log can distinguish explicit-keyword runs from
    # implicit-target_titles runs.
    summary["portal_search_used_fallback_keywords"] = used_fallback

    max_serp_queries = portal_cfg.get("max_serp_queries", 30)

    # Build DataForSEO source if configured (cheapest SERP backend)
    dataforseo_source = None
    dfse_cfg = config.get("sources", {}).get("dataforseo", {})
    dfse_api_key = get_secret("sources.dataforseo.api_key", config=config) or ""
    if dfse_cfg.get("enabled") and dfse_api_key:
        from job_finder.sources.dataforseo_source import DataForSEOSource

        dataforseo_source = DataForSEOSource(
            api_key=dfse_api_key,
            depth=10,  # site: queries return few results; 10 is plenty
            priority=dfse_cfg.get("priority", 1),
            poll_interval_seconds=dfse_cfg.get("poll_interval_seconds", 30),
            poll_timeout_seconds=dfse_cfg.get("poll_timeout_seconds", 360),
        )

    # Build Google CSE source as the free SERP fallback. Only constructed when
    # caller permits CSE this run (include_cse=True) — load-bearing decision #8
    # caps CSE to one of the 3x/day ingestion slots. fetch_all_portals prefers
    # dataforseo_source when both backends are passed, so it's safe to construct
    # both here.
    google_cse_source = None
    if include_cse:
        cse_cfg = config.get("sources", {}).get("google_cse", {})
        if cse_cfg.get("enabled"):
            cse_api_key = get_secret("sources.google_cse.api_key", config=config) or ""
            cse_id = get_secret("sources.google_cse.cse_id", config=config) or ""
            if cse_api_key and cse_id:
                from job_finder.sources.google_cse_source import GoogleCSESource

                google_cse_source = GoogleCSESource(
                    api_key=cse_api_key,
                    cse_id=cse_id,
                    db_path=db_path,
                )

    # Stage 7.1: inject keyring-resolved creds for USAJobs/Adzuna/Jooble. The
    # canonical names mirror the nested config tree, so get_secret's
    # config-yaml fallback walks the same path and finds existing plaintext
    # for users who haven't migrated to keyring yet. fetch_all_portals's
    # contract (it reads creds from the portal_config subtree) is unchanged
    # — we just give it a copy with keyring values injected.
    portal_cfg_with_creds = _inject_portal_search_creds(portal_cfg, config)

    # Stage 7.6 title-gate config. Read from `profile` so the gate uses the
    # same target_titles the ATS scanners already enforce. `exclusions` is
    # an optional dict whose `title_keywords` list mirrors the scanner-side
    # exclusion signature (`scan_lever(slug, target_titles, exclusions)`).
    # When target_titles is empty (caller bypassed validate_target_titles)
    # the gate is a no-op — preserves legacy behavior.
    profile = config.get("profile") or {}
    gate_target_titles = list(profile.get("target_titles") or [])
    gate_exclusions = list(
        ((profile.get("exclusions") or {}).get("title_keywords") or [])
    )

    try:
        from job_finder.sources.portal_search_source import fetch_all_portals

        jobs = fetch_all_portals(
            keywords,
            dataforseo_source=dataforseo_source,
            max_serp_queries=max_serp_queries,
            portal_config=portal_cfg_with_creds,
            google_cse_source=google_cse_source,
            target_titles=gate_target_titles,
            exclusions=gate_exclusions,
        )
        summary["portal_search_fetched"] = len(jobs)
        # Per-portal breakdown: each fetcher tags Job.source with a unique
        # `portal_<name>` label, so a simple group-by on the merged list
        # recovers per-source attribution without changing fetch_all_portals's
        # contract. Closes the deferred per-portal observability follow-up
        # from Stage 7.9. Only portals that actually returned a job appear;
        # zero-yield portals are absent (reader uses .get(k, 0)).
        from collections import Counter

        per_portal = Counter(j.source for j in jobs if j.source)
        for portal_name, count in per_portal.items():
            summary[f"{portal_name}_fetched"] = count
        return jobs
    except Exception as e:
        error_msg = str(e)
        summary.setdefault("portal_search_errors", []).append(error_msg)
        logger.warning("Portal search failed: %s", error_msg)
        return []


def _inject_portal_search_creds(portal_cfg: dict, config: dict) -> dict:
    """Return a copy of portal_cfg with USAJobs/Adzuna/Jooble creds resolved.

    Stage 7.1: the Settings UI writes these creds to OS keyring under
    canonical names mirroring the nested config path. The Settings parser
    clears the plaintext leaf when keyring write succeeds — so reading
    plaintext from portal_cfg alone returns empty for keyring-stored creds.

    get_secret() consults env → keyring → config-yaml plaintext in that
    order, so a single call resolves either source. The returned dict is
    a shallow copy with per-portal subtrees deep-copied only when modified.
    """
    augmented = dict(portal_cfg)

    usajobs = dict(augmented.get("usajobs") or {})
    if usajobs.get("enabled"):
        ua = get_secret(
            "sources.portal_search.usajobs.user_agent_email", config=config
        ) or usajobs.get("user_agent_email", "") or ""
        ak = get_secret(
            "sources.portal_search.usajobs.authorization_key", config=config
        ) or usajobs.get("authorization_key", "") or ""
        usajobs["user_agent_email"] = ua
        usajobs["authorization_key"] = ak
        augmented["usajobs"] = usajobs

    adzuna = dict(augmented.get("adzuna") or {})
    if adzuna.get("enabled"):
        aid = get_secret(
            "sources.portal_search.adzuna.app_id", config=config
        ) or adzuna.get("app_id", "") or ""
        akey = get_secret(
            "sources.portal_search.adzuna.app_key", config=config
        ) or adzuna.get("app_key", "") or ""
        adzuna["app_id"] = aid
        adzuna["app_key"] = akey
        augmented["adzuna"] = adzuna

    jooble = dict(augmented.get("jooble") or {})
    if jooble.get("enabled"):
        jkey = get_secret(
            "sources.portal_search.jooble.api_key", config=config
        ) or jooble.get("api_key", "") or ""
        jooble["api_key"] = jkey
        augmented["jooble"] = jooble

    return augmented


def _submit_dataforseo_tasks(
    config: dict, summary: dict
) -> tuple[list[str], Optional["DataForSEOSource"]]:
    """Submit DataForSEO tasks early (non-blocking ~2s).

    Returns (task_ids, source_instance). The source is returned so
    _collect_dataforseo_results can reuse it without re-extracting config.
    Returns ([], None) if source is disabled, unconfigured, or submission fails.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        Tuple of (list of task ID strings, DataForSEOSource instance or None).
    """
    dataforseo_config = config.get("sources", {}).get("dataforseo", {})
    if not dataforseo_config.get("enabled", False):
        logger.debug("DataForSEO source disabled in config.")
        return [], None

    api_key = get_secret("sources.dataforseo.api_key", config=config) or ""
    if not api_key:
        msg = "DataForSEO API key not configured"
        summary["dataforseo_errors"].append(msg)
        logger.warning(msg)
        return [], None

    queries = dataforseo_config.get("queries", [])
    if not queries:
        logger.debug("No DataForSEO queries configured.")
        return [], None

    try:
        from job_finder.sources.dataforseo_source import DataForSEOSource

        source = DataForSEOSource(
            api_key,
            max_age_days=dataforseo_config.get("max_age_days", 7),
            depth=dataforseo_config.get("depth", 200),
            priority=dataforseo_config.get("priority", 1),
            poll_interval_seconds=dataforseo_config.get("poll_interval_seconds", 30),
            poll_timeout_seconds=dataforseo_config.get("poll_timeout_seconds", 360),
        )
        task_ids = source.submit_tasks(queries)
        if not task_ids:
            msg = f"DataForSEO: submit returned no task IDs for {len(queries)} queries (all tasks rejected)"
            summary["dataforseo_errors"].append(msg)
            logger.warning(
                "DataForSEO: submit_tasks returned no task IDs for %d queries", len(queries)
            )
            return [], None
        logger.info("DataForSEO: submitted %d tasks (non-blocking)", len(task_ids))
        return task_ids, source

    except Exception as e:
        summary["dataforseo_errors"].append(str(e))
        logger.warning("DataForSEO task submission failed: %s", e)
        return [], None


def _collect_dataforseo_results(
    source: Optional["DataForSEOSource"],
    task_ids: list[str],
    summary: dict,
) -> list[Job]:
    """Collect results for previously submitted DataForSEO tasks.

    Args:
        source: DataForSEOSource instance returned by _submit_dataforseo_tasks,
                or None if submission was skipped/failed.
        task_ids: Task UUIDs returned by submit_tasks().
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects. Empty if source is None or task_ids is empty.
    """
    if not source or not task_ids:
        return []

    try:
        t0 = time.monotonic()
        jobs = source.collect_results(task_ids)
        elapsed = time.monotonic() - t0
        summary["dataforseo_fetched"] = len(jobs)
        logger.info("DataForSEO collect: %.1fs, %d jobs", elapsed, len(jobs))
        return jobs

    except Exception as e:
        summary["dataforseo_errors"].append(str(e))
        logger.warning("DataForSEO result collection failed: %s", e)
        return []


def _score_and_persist(
    job: Job, scorer: JobScorer, conn, summary: dict, new_job_keys: list[str]
) -> None:
    """Score a single job and persist it. Errors are logged but not re-raised.

    Per-job error isolation: if scoring or persistence fails for one job,
    processing continues for the remaining jobs.

    Args:
        job: Job object to score and persist.
        scorer: Initialized JobScorer instance.
        conn: Open sqlite3 connection.
        summary: Mutable summary dict to update.
        new_job_keys: Mutable list; new job dedup_keys are appended here.
    """
    try:
        # Score the job (updates job.score and job.score_breakdown in place)
        scored_job = scorer.score_jobs([job])
        if scored_job:
            job = scored_job[0]
            summary["jobs_scored"] += 1

        # Persist (upsert handles dedup by dedup_key)
        is_new = upsert_job(conn, job)
        if is_new:
            summary["jobs_new"] += 1
            new_job_keys.append(job.dedup_key)
        else:
            summary["jobs_updated"] += 1

        # Company auto-population: create/update company record for every job
        _upsert_job_company(conn, job)

    except Exception as e:
        error_msg = f"{job.title} @ {job.company}: {e}"
        summary["job_errors"].append(error_msg)
        logger.warning("Failed to score/persist job '%s' at '%s': %s", job.title, job.company, e)


def _touch_existing_job(job: Job, conn: sqlite3.Connection, summary: dict) -> None:
    """Lightweight update for already-known jobs: touch last_seen and merge source/source_url.

    Skips scoring, full upsert merge logic, and company upsert for jobs whose
    dedup_key already exists in the DB and whose incoming data carries no new
    salary information worth merging. Updates last_seen timestamp and merges
    the incoming job.source and job.source_url into the existing JSON columns
    without duplicates.

    Args:
        job: Incoming Job with a dedup_key already present in DB.
        conn: Open sqlite3 connection.
        summary: Mutable summary dict — increments ``jobs_touch_only``.
    """
    conn.execute(
        """UPDATE jobs
           SET last_seen = ?,
               sources = (
                   SELECT json_group_array(value)
                   FROM (
                       -- UNION (not UNION ALL) provides set-semantics, deduplicating sources
                       SELECT value FROM json_each(sources)
                       UNION
                       SELECT value FROM json_each(json_array(?))
                   )
               ),
               source_urls = (
                   SELECT json_group_array(value)
                   FROM (
                       -- UNION (not UNION ALL) provides set-semantics, deduplicating URLs
                       -- CASE WHEN handles empty source_url: both ? bind job.source_url
                       SELECT value FROM json_each(source_urls)
                       UNION
                       SELECT value FROM json_each(CASE WHEN ? != '' THEN json_array(?) ELSE '[]' END)
                   )
               )
           WHERE dedup_key = ?""",
        (utc_now_iso(), job.source, job.source_url, job.source_url, job.dedup_key),
    )
    conn.commit()
    summary["jobs_touch_only"] += 1


def _upsert_job_company(conn, job: Job) -> None:
    """Create or update the company record associated with a job.

    Non-fatal: any error is logged at DEBUG level and does not crash ingestion.
    Lazy import to avoid circular: ats_scanner → dedup_normalizer → pipeline_runner.

    Args:
        conn: Open sqlite3 connection.
        job: Job object whose company should be upserted.
    """
    try:
        from job_finder.web.ats_company import upsert_company
        from job_finder.web.ats_detection import extract_ats_from_urls
    except ImportError:
        return

    try:
        # job.source_url is a single URL string; wrap in list for extract_ats_from_urls
        source_url = job.source_url or ""
        source_urls = [source_url] if source_url else []
        ats_platform, ats_slug = extract_ats_from_urls(source_urls)
        company_id = upsert_company(
            conn,
            name=job.company,
            ats_platform=ats_platform,
            ats_slug=ats_slug,
            ats_probe_status="pending",
        )
        if company_id:
            conn.execute(
                "UPDATE jobs SET company_id = ? WHERE dedup_key = ?",
                (company_id, job.dedup_key),
            )
            conn.commit()
    except Exception as company_err:
        logger.debug(
            "Company upsert failed for '%s' (non-fatal): %s",
            job.company,
            company_err,
        )


def _prune_stale_data(conn: sqlite3.Connection, lookback_days: int = 7) -> None:
    """Prune stale entries from both the runs and email_parse_log tables.

    Covers two tables:

    **runs table** — accumulates ~1,000 rows/day from parse_failure entries:
    - parse_failure rows older than 30 days are deleted
    - All rows older than 90 days are deleted

    **email_parse_log table** — stores per-message Gmail dedup rows and
    run-level summary rows (sender='gmail', message_id='gmail_run_...');
    both are pruned at the same TTL:
    - Rows with sender='gmail' older than ``max(lookback_days * 2, 14)`` days
      are deleted. The TTL is at least 14 days and scales with lookback_days
      so that dedup records are never pruned while Gmail still returns those
      emails on the next sync.

    Non-fatal: any error is logged at Warning level and does not interrupt
    ingestion.

    Args:
        conn: Active SQLite connection.
        lookback_days: Gmail lookback window (from config). Used to compute
            the email_parse_log TTL as max(lookback_days * 2, 14).
    """
    email_parse_log_ttl = max(lookback_days * 2, 14)
    try:
        conn.execute(
            "DELETE FROM runs"
            " WHERE timestamp < datetime('now', '-30 days')"
            " AND source LIKE '%parse_failure%'"
        )
        conn.execute("DELETE FROM runs WHERE timestamp < datetime('now', '-90 days')")
        # Trim email_parse_log rows (both per-message dedup rows and run-level
        # summary rows with sender='gmail').  TTL scales with lookback_days so
        # dedup records are never expired while Gmail still returns those emails.
        # At ~300 rows/run × 3 runs/day that's ~109K rows/year without pruning.
        conn.execute(
            "DELETE FROM email_parse_log"
            " WHERE processed_at < datetime('now', ?)"
            " AND sender = 'gmail'",
            (f"-{email_parse_log_ttl} days",),
        )
        conn.commit()
        logger.debug(
            "Pruned stale runs and email_parse_log rows (email_parse_log TTL: %d days)",
            email_parse_log_ttl,
        )
    except Exception as e:
        logger.warning("Failed to prune stale data: %s", e)


def _log_to_email_parse_log(
    conn: sqlite3.Connection,
    message_id: str,
    sender: str,
    jobs_found: int,
    error: str | None,
) -> None:
    """Insert a record into email_parse_log.

    Uses INSERT OR IGNORE so re-runs with the same message_id don't fail.

    Args:
        conn: Active SQLite connection.
        message_id: Unique ID for this log entry (run-level or message-level).
        sender: Source label (e.g., "gmail", "no-reply@ziprecruiter.com").
        jobs_found: Number of jobs parsed from this email/run.
        error: Error message if parsing failed, else None.
    """
    try:
        conn.execute(
            """INSERT OR IGNORE INTO email_parse_log
               (message_id, sender, processed_at, jobs_found, error)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, sender, datetime.now().isoformat(), jobs_found, error),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to write to email_parse_log: %s", e)
