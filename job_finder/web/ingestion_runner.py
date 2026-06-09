"""Source-fetch and persistence helpers for the ingestion pipeline.

All private helpers used by run_ingestion live here. They are re-exported
from pipeline_runner so existing patch paths (job_finder.web.pipeline_runner.*)
continue to work without changes.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from job_finder.sources.dataforseo_source import DataForSEOSource

from job_finder.config import DEFAULT_LOOKBACK_DAYS
from job_finder.db import upsert_job
from job_finder.json_utils import utc_now_iso
from job_finder.models import Job
from job_finder.scoring.scorer import JobScorer
from job_finder.secrets import get_secret
from job_finder.web.db_helpers import standalone_connection

try:
    from job_finder.sources.gmail_source import GmailSource
except ImportError:
    GmailSource = None  # type: ignore[assignment,misc]

try:
    from job_finder.sources.imap_source import ImapSource
except ImportError:
    ImapSource = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _apply_title_gate(jobs: list[Job], config: dict, source_label: str) -> list[Job]:
    """Apply the Stage 7.6 title-gate invariant to a non-portal ingestion source.

    Reads ``profile.target_titles`` + ``profile.exclusions.title_keywords`` from
    config and applies ``_title_matches`` (word-boundary regex, the same helper
    every ATS scanner enforces inline and that Stage 7.6 added to
    ``fetch_all_portals``). When ``target_titles`` is empty this is a no-op —
    same bypass semantics as ``validate_target_titles`` and the Stage 7.6 gate.

    Empirical context: gmail/serpapi/dataforseo/thordata historically passed
    ``_title_matches`` at only 26-72%; the rest were off-target rows the
    upstream ``q=``/alert-config could not filter and that reached scoring
    anyway. This gate closes that downstream leak with one consistent rule.
    """
    if not jobs:
        return jobs
    profile = config.get("profile") or {}
    target_titles = list(profile.get("target_titles") or [])
    if not target_titles:
        return jobs
    exclusions = list((profile.get("exclusions") or {}).get("title_keywords") or [])

    # Lazy import to avoid pulling ats_platforms (and its heavyweight deps)
    # at module import time. Same pattern as fetch_all_portals.
    from job_finder.web.ats_platforms import _title_matches

    pre = len(jobs)
    filtered = [j for j in jobs if _title_matches(j.title, target_titles, exclusions)]
    post = len(filtered)
    if post != pre:
        logger.info(
            "title-gate %s: %d → %d jobs (target_titles=%d, exclusions=%d)",
            source_label,
            pre,
            post,
            len(target_titles),
            len(exclusions),
        )
    return filtered


def _user_identifiers(config: dict) -> tuple[str, ...]:
    """Personal identifiers to redact from captured email bodies, sourced from config.

    sources.imap.email is the verified real key (ingestion_runner.py:293). profile.name
    is included when present. Returns () when neither exists.
    """
    idents: list[str] = []
    email = config.get("sources", {}).get("imap", {}).get("email")
    if email:
        idents.append(email)
    name = config.get("profile", {}).get("name")
    if name:
        idents.append(name)
    return tuple(idents)


def _record_email_extractions(source, conn, config: dict) -> None:
    """Drain a source's accumulated extraction_records into the health monitor.

    Never raises (record_extraction swallows its own errors); observability must
    not break ingestion.
    """
    from job_finder.web.autoheal.health_monitor import record_extraction

    idents = _user_identifiers(config)
    for rec in getattr(source, "extraction_records", []):
        record_extraction(
            conn,
            rec["label"],
            "email",
            rec["raw_text"],
            rec["job_count"],
            scrub_identifiers=idents,
            detect=True,
        )


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

    run_id = f"gmail_run_{utc_now_iso()}"
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
                    (utc_now_iso(), f"{domain}_parse_failure"),
                )
                conn.commit()
                logger.debug("Zero-job email routed to activity feed: %s", fail_sender)
            except Exception as e:
                logger.warning("Failed to log parse failure to runs: %s", e)

        # --- Drain per-email extraction records into the health monitor ---
        _record_email_extractions(source, conn, config)

        # Apply title-gate AFTER message-level dedup writes (above) so the
        # email_parse_log message-ID inserts still cover every fetched message
        # — gate filters Job objects, not message tracking. summary +
        # run-level log use the post-gate count (matches Stage 7.6 portal_search
        # semantics: dashboard's *_fetched reflects what reached downstream).
        jobs = _apply_title_gate(jobs, config, "gmail")
        summary["gmail_fetched"] = len(jobs)

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


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Per-source contract for the simple-fetch driver.

    Gmail is NOT a SourceSpec — its email_parse_log dedup pass diverges
    enough that the explicit shape stays clearer than parameterizing it.
    DataForSEO is also NOT a SourceSpec — its submit/collect split has
    no fit with the single-call contract.
    """

    name: str
    secret_path: str
    build_source: Callable[[dict, str], object]
    require_secret: bool = True
    require_queries: bool = True
    extract_jobs: Callable[[object, dict], list[Job]] = field(
        default=lambda src, source_cfg: src.fetch_jobs(source_cfg.get("queries", []))
    )
    validate_config: Callable[[dict], str | None] = field(default=lambda _: None)


def _run_simple_source(
    spec: SourceSpec, config: dict, summary: dict, post_extract=None
) -> list[Job]:
    """Fetch from a simple source with the standard enabled/secret/error envelope.

    Mirrors the original inline shape of ``_fetch_serpapi``/``_fetch_thordata``/
    ``_fetch_imap``: enabled-check → validate_config → secret → queries → build →
    extract → post_extract hook → title-gate → summary update. Errors during
    build/extract are isolated per-source (caught, appended to
    ``summary[f"{spec.name}_errors"]``). The optional ``post_extract(source)``
    hook is invoked after extract; its errors are swallowed so observability
    never breaks ingestion.
    """
    source_cfg = config.get("sources", {}).get(spec.name, {})
    if not source_cfg.get("enabled", False):
        logger.debug("%s source disabled in config.", spec.name)
        return []

    err = spec.validate_config(source_cfg)
    if err is not None:
        summary[f"{spec.name}_errors"].append(err)
        logger.warning(err)
        return []

    secret = ""
    if spec.require_secret:
        secret = get_secret(spec.secret_path, config=config) or ""
        if not secret:
            msg = f"{spec.name} key not configured"
            summary[f"{spec.name}_errors"].append(msg)
            logger.warning(msg)
            return []

    if spec.require_queries:
        queries = source_cfg.get("queries", [])
        if not queries:
            logger.debug("No %s queries configured.", spec.name)
            return []

    try:
        source = spec.build_source(source_cfg, secret)
        jobs = spec.extract_jobs(source, source_cfg)
        if post_extract is not None:
            try:
                post_extract(source)
            except Exception:
                logger.exception("post_extract hook failed for %s", spec.name)
        jobs = _apply_title_gate(jobs, config, spec.name)
        summary[f"{spec.name}_fetched"] = len(jobs)
        logger.info("%s: fetched %d jobs", spec.name, len(jobs))
        return jobs
    except Exception as e:
        error_msg = str(e)
        summary[f"{spec.name}_errors"].append(error_msg)
        logger.warning("%s ingestion failed: %s", spec.name, error_msg)
        return []


def _build_serpapi_source(source_cfg: dict, secret: str) -> object:
    from job_finder.sources.serpapi_source import SerpAPISource

    return SerpAPISource(secret, max_pages=source_cfg.get("max_pages", 5))


def _build_thordata_source(source_cfg: dict, secret: str) -> object:
    from job_finder.sources.thordata_source import ThordataSource

    return ThordataSource(secret, max_age_days=source_cfg.get("max_age_days", 3))


def _build_imap_source(source_cfg: dict, secret: str) -> object:
    # ImapSource is None when the optional imap_source module fails to import.
    # Raising here lets the driver's try/except convert it into the same
    # empty-list + summary-error envelope as any other build failure.
    if ImapSource is None:
        raise RuntimeError("ImapSource import failed; skipping IMAP fetch")
    return ImapSource(
        host=source_cfg.get("host", "imap.gmail.com"),
        port=source_cfg.get("port", 993),
        email_address=source_cfg.get("email", ""),
        app_password=secret,
        folder=source_cfg.get("folder", "INBOX"),
    )


_SERPAPI_SPEC = SourceSpec(
    name="serpapi",
    secret_path="sources.serpapi.api_key",  # noqa: S106 — config path, not a secret value
    build_source=_build_serpapi_source,
)

_THORDATA_SPEC = SourceSpec(
    name="thordata",
    secret_path="sources.thordata.api_key",  # noqa: S106 — config path, not a secret value
    build_source=_build_thordata_source,
)

_IMAP_SPEC = SourceSpec(
    name="imap",
    secret_path="sources.imap.app_password",  # noqa: S106 — config path, not a secret value
    build_source=_build_imap_source,
    require_queries=False,
    # IMAP's fetch_jobs() returns a (jobs, ids) tuple and takes no queries arg.
    extract_jobs=lambda src, source_cfg: src.fetch_jobs()[0],
    # IMAP requires `email` in the config in addition to the app_password
    # secret; surface a non-silent error if it's missing.
    validate_config=lambda cfg: "sources.imap.email is required" if not cfg.get("email") else None,
)


def _fetch_serpapi(config: dict, summary: dict) -> list[Job]:
    return _run_simple_source(_SERPAPI_SPEC, config, summary)


def _fetch_thordata(config: dict, summary: dict) -> list[Job]:
    return _run_simple_source(_THORDATA_SPEC, config, summary)


def _fetch_imap(config: dict, summary: dict, db_path: str = "") -> list[Job]:
    """Fetch IMAP jobs and drain extraction_records into the health monitor.

    db_path is passed explicitly to avoid the config-path divergence hazard —
    standalone_connection here uses the same db_path as the pipeline caller,
    not whatever the config's db section says.
    """
    if db_path:

        def _drain(source):
            with standalone_connection(db_path) as c:
                _record_email_extractions(source, c, config)

        return _run_simple_source(_IMAP_SPEC, config, summary, post_extract=_drain)
    return _run_simple_source(_IMAP_SPEC, config, summary)


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
            logger.info("Portal search: no keywords and no profile.target_titles, skipping")
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

    # Build DataForSEO source if configured (cheapest SERP backend).
    # Gate-before-fetch: resolving the secret unconditionally would trip the
    # one-time "plaintext at rest" warning even when the source is disabled,
    # so we mirror the ordering used by _submit_dataforseo_tasks and the CSE
    # branch below — a disabled source must touch no credentials.
    dataforseo_source = None
    dfse_cfg = config.get("sources", {}).get("dataforseo", {})
    if dfse_cfg.get("enabled"):
        dfse_api_key = get_secret("sources.dataforseo.api_key", config=config) or ""
        if dfse_api_key:
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
    gate_exclusions = list((profile.get("exclusions") or {}).get("title_keywords") or [])

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
        ua = (
            get_secret("sources.portal_search.usajobs.user_agent_email", config=config)
            or usajobs.get("user_agent_email", "")
            or ""
        )
        ak = (
            get_secret("sources.portal_search.usajobs.authorization_key", config=config)
            or usajobs.get("authorization_key", "")
            or ""
        )
        usajobs["user_agent_email"] = ua
        usajobs["authorization_key"] = ak
        augmented["usajobs"] = usajobs

    adzuna = dict(augmented.get("adzuna") or {})
    if adzuna.get("enabled"):
        aid = (
            get_secret("sources.portal_search.adzuna.app_id", config=config)
            or adzuna.get("app_id", "")
            or ""
        )
        akey = (
            get_secret("sources.portal_search.adzuna.app_key", config=config)
            or adzuna.get("app_key", "")
            or ""
        )
        adzuna["app_id"] = aid
        adzuna["app_key"] = akey
        augmented["adzuna"] = adzuna

    jooble = dict(augmented.get("jooble") or {})
    if jooble.get("enabled"):
        jkey = (
            get_secret("sources.portal_search.jooble.api_key", config=config)
            or jooble.get("api_key", "")
            or ""
        )
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
    config: dict | None = None,
) -> list[Job]:
    """Collect results for previously submitted DataForSEO tasks.

    Args:
        source: DataForSEOSource instance returned by _submit_dataforseo_tasks,
                or None if submission was skipped/failed.
        task_ids: Task UUIDs returned by submit_tasks().
        summary: Mutable summary dict to update.
        config: Optional full config dict. When provided, ``_apply_title_gate``
            uses ``profile.target_titles`` + ``profile.exclusions.title_keywords``
            to filter results before persistence (Stage 7.6 follow-up; historical
            pass rate against this gate was 60.4%). When omitted (legacy callers
            in tests), the gate is skipped — same bypass semantics as everywhere
            else in this module.

    Returns:
        List of Job objects. Empty if source is None or task_ids is empty.
    """
    if not source or not task_ids:
        return []

    try:
        t0 = time.monotonic()
        jobs = source.collect_results(task_ids)
        elapsed = time.monotonic() - t0
        if config is not None:
            jobs = _apply_title_gate(jobs, config, "dataforseo")
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

        # Persist (upsert handles dedup by dedup_key). Phase 48.07: the Job
        # shim is gone — construct a ParsedJob here and forward scoring as
        # explicit kwargs (score/score_breakdown are not parser-owned).
        from job_finder.parsed_job import DenylistedCompanyError, ParsedJob

        try:
            parsed = ParsedJob.from_job(job)
        except DenylistedCompanyError:
            # Preserve the pre-48.07 shim early-return: a denylisted company
            # is reported as "unchanged" so the per-job error counter does
            # not fire and ingestion summary counts stay identical.
            return
        result = upsert_job(
            conn,
            parsed,
            score=job.score,
            score_breakdown=job.score_breakdown,
        )
        if result.kind == "inserted":
            summary["jobs_new"] += 1
            # #223: enqueue the PERSISTED key (clean_title-normalized) so the
            # scorer's lookup hits. Job.dedup_key normalizes the raw title and
            # diverges from ParsedJob's key whenever clean_title strips a
            # req-id / location suffix / dash qualifier / logo letter.
            new_job_keys.append(result.dedup_key)
        elif result.kind == "updated":
            summary["jobs_updated"] += 1
        elif result.kind == "touched":
            # Re-sighting of a known job from another feed: last_seen + source
            # union only (D-15 folded the former touch-path helper into upsert).
            summary["jobs_touch_only"] = summary.get("jobs_touch_only", 0) + 1

        # Company auto-population: create/update company record for every job
        _upsert_job_company(conn, job)

    except Exception as e:
        error_msg = f"{job.title} @ {job.company}: {e}"
        summary["job_errors"].append(error_msg)
        logger.warning("Failed to score/persist job '%s' at '%s': %s", job.title, job.company, e)


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
            (message_id, sender, utc_now_iso(), jobs_found, error),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to write to email_parse_log: %s", e)
