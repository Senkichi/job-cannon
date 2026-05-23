"""Per-source effectiveness benchmark.

Stage 0 of the no-key compensation plan. CLI that fetches a representative
sample from every configured job source, scores each source on raw / parse /
title-match / novel / overlap counts plus wall-clock latency, and writes a
markdown report. Run once before any compensation stage lands to establish
a baseline; re-run after each stage and commit the dated report under
``.planning/SOURCE-BENCHMARK-{date}-{stage}.md``.

The script reads sources directly (not via the live ingestion pipeline) so
no jobs are persisted, no scoring is triggered, and no scheduler state is
mutated. The DB is opened read-only.

Usage:
    uv run python scripts/benchmark_sources.py [--no-paid]
                                               [--output PATH]
                                               [--config PATH]

``--no-paid`` simulates the fresh-user scenario by skipping serpapi /
thordata / dataforseo / SERP-backed portal queries even when they are
enabled and credentialed in config.yaml.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from job_finder.models import Job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceResult:
    """One row of the benchmark report.

    Immutable so the runner can collect a list of these and reorder without
    risk of mutation in the markdown formatter.
    """

    source: str
    raw_count: int
    parse_ok: int
    title_match_count: int
    novel_count: int
    overlap_pct: float
    fetch_seconds: float
    sample_titles: tuple[str, ...]
    notes: str


def _title_passes(title: str, target_titles: list[str]) -> bool:
    """Apply the canonical title matcher with empty exclusions.

    Exclusions are intentionally NOT applied: the benchmark should measure
    how much each source delivers irrespective of a user's curated
    exclusion list, otherwise drift in exclusions would muddle source
    comparisons across runs.
    """
    if not target_titles:
        # No filter requested — every title passes (defensive; main() rejects
        # empty target_titles before reaching this code path).
        return True
    from job_finder.web.ats_platforms import _title_matches

    return _title_matches(title, target_titles, [])


def benchmark_one_source(
    name: str,
    fetch_fn: Callable[[], list[Job]],
    *,
    target_titles: list[str],
    existing_keys: set[str],
) -> SourceResult:
    """Time and characterise one source's output without mutating anything.

    Args:
        name: Label for the report row (e.g. "gmail", "portal_remoteok").
        fetch_fn: Zero-arg callable that returns a list of :class:`Job` objects.
            Should be a closure binding any config / credentials the source
            needs. Any exception is caught and surfaced in ``notes``.
        target_titles: Used to compute ``title_match_count`` via the canonical
            ``_title_matches`` matcher.
        existing_keys: Set of dedup_keys already in the DB; jobs whose
            dedup_key is in this set count toward overlap, not novel.

    Returns:
        A populated :class:`SourceResult`. ``raw_count``/``novel_count`` are
        zero and ``notes`` carries the exception summary if ``fetch_fn``
        raises.
    """
    t0 = time.monotonic()
    notes = ""
    jobs: list[Job] = []
    try:
        jobs = fetch_fn() or []
    except Exception as exc:
        notes = f"{type(exc).__name__}: {exc}"
        logger.warning("Benchmark fetch failed for %s: %s", name, notes)
        logger.debug("Traceback for %s:\n%s", name, traceback.format_exc())

    elapsed = round(time.monotonic() - t0, 2)
    raw = len(jobs)
    parse_ok = raw  # fetch_fn returns parsed Job objects already
    title_match = sum(1 for j in jobs if _title_passes(j.title, target_titles))
    novel = sum(1 for j in jobs if j.dedup_key not in existing_keys)
    overlap_pct = 0.0 if raw == 0 else round(100.0 * (raw - novel) / raw, 1)
    sample = tuple(f"{j.title} @ {j.company}" for j in jobs[:5])

    return SourceResult(
        source=name,
        raw_count=raw,
        parse_ok=parse_ok,
        title_match_count=title_match,
        novel_count=novel,
        overlap_pct=overlap_pct,
        fetch_seconds=elapsed,
        sample_titles=sample,
        notes=notes,
    )


def load_existing_dedup_keys(db_path: str) -> set[str]:
    """Read all ``dedup_key`` values from the jobs table into a set.

    Used to compute per-source ``novel_count`` (items not already in the DB).
    Opens the connection read-only and returns an empty set if the file is
    missing, so the benchmark can still produce a report on a fresh install
    where every fetched job is by definition novel.

    Args:
        db_path: Filesystem path to ``jobs.db``.

    Returns:
        Set of dedup_key strings. Empty set if the file does not exist or
        the jobs table is empty / absent.
    """
    import os

    if not os.path.exists(db_path):
        return set()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute("SELECT dedup_key FROM jobs").fetchall()
        except sqlite3.OperationalError as e:
            # Table missing on a partially-initialized DB.
            logger.warning("Could not read dedup_key from jobs table: %s", e)
            return set()
        return {row[0] for row in rows if row[0] is not None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------


def format_markdown_report(
    results: list[SourceResult],
    *,
    target_titles: list[str],
    existing_count: int,
    no_paid: bool,
) -> str:
    """Render benchmark results to the committed-baseline markdown shape.

    The columns and section headings here are the post-impl comparison
    surface — downstream stages will diff their report against this one,
    so the column set must be stable.
    """
    from datetime import datetime

    lines: list[str] = []
    banner = " (no-paid simulation)" if no_paid else ""
    lines.append(f"# Source Benchmark — Baseline{banner} ({datetime.now().date().isoformat()})")
    lines.append("")
    lines.append(f"- target_titles: {', '.join(target_titles) or '(empty)'}")
    lines.append(f"- existing jobs in DB: {existing_count}")
    lines.append(f"- no-paid mode: {'yes' if no_paid else 'no'}")
    lines.append("")
    lines.append("## Per-source counts")
    lines.append("")
    lines.append(
        "| source | raw | parse_ok | title_match | novel | overlap_pct | fetch_s | notes |"
    )
    lines.append(
        "|--------|----:|---------:|------------:|------:|------------:|--------:|-------|"
    )
    for r in results:
        notes_cell = r.notes.replace("|", "\\|") if r.notes else "-"
        lines.append(
            f"| {r.source} | {r.raw_count} | {r.parse_ok} | {r.title_match_count} "
            f"| {r.novel_count} | {r.overlap_pct}% | {r.fetch_seconds} | {notes_cell} |"
        )
    lines.append("")
    lines.append("## Sample titles (top 5 per source)")
    lines.append("")
    for r in results:
        lines.append(f"### {r.source}")
        if not r.sample_titles:
            lines.append("- (no results)")
        else:
            for title in r.sample_titles:
                lines.append(f"- {title}")
        lines.append("")

    # Recorded for the post-impl comparison (open question Q6 from
    # NO-KEY-COMPENSATION-PLAN.md). Not enforced as a CLI gate; lives in the
    # report so future stages know what target to read against.
    lines.append("## Comparison threshold (Q6)")
    lines.append("")
    lines.append(
        "After Stage 2-5 land, re-run this benchmark and check that the sum of"
        " free-source `novel` counts (gmail + imap + portal_*) is at least 80%"
        " of the sum of paid-source `novel` counts (serpapi + thordata +"
        " dataforseo) in this baseline."
    )
    lines.append(
        " 80% is a placeholder agreed during planning (Q6, user invited"
        " revision once baseline numbers landed) — revisit if the baseline"
        " makes it look unreachable or trivial."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-source benchmark adapters
#
# Each adapter is a module-level function so tests can monkeypatch it without
# touching the live ingestion pipeline. The adapter returns a list[Job]; the
# benchmark runner times and characterises that list.
# ---------------------------------------------------------------------------


def _fetch_gmail_for_benchmark(cfg: dict) -> list[Job]:
    gmail_cfg = cfg.get("sources", {}).get("gmail", {})
    if not gmail_cfg.get("enabled", False):
        return []
    from job_finder.sources.gmail_source import GmailSource

    source = GmailSource()
    jobs, _ = source.fetch_jobs(
        lookback_days=gmail_cfg.get("lookback_days", 7),
        processed_message_ids=set(),
    )
    return jobs


def _fetch_imap_for_benchmark(cfg: dict) -> list[Job]:
    imap_cfg = cfg.get("sources", {}).get("imap", {})
    if not imap_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.imap_source import ImapSource

    email = imap_cfg.get("email", "")
    app_password = get_secret("sources.imap.app_password", config=cfg) or ""
    if not email or not app_password:
        return []
    source = ImapSource(
        host=imap_cfg.get("host", "imap.gmail.com"),
        port=imap_cfg.get("port", 993),
        email_address=email,
        app_password=app_password,
        folder=imap_cfg.get("folder", "INBOX"),
    )
    jobs, _ = source.fetch_jobs()
    return jobs


def _fetch_serpapi_for_benchmark(cfg: dict) -> list[Job]:
    serpapi_cfg = cfg.get("sources", {}).get("serpapi", {})
    if not serpapi_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.serpapi_source import SerpAPISource

    api_key = get_secret("sources.serpapi.api_key", config=cfg) or ""
    queries = serpapi_cfg.get("queries", [])
    if not api_key or not queries:
        return []
    source = SerpAPISource(api_key, max_pages=serpapi_cfg.get("max_pages", 5))
    return source.fetch_jobs(queries)


def _fetch_thordata_for_benchmark(cfg: dict) -> list[Job]:
    thordata_cfg = cfg.get("sources", {}).get("thordata", {})
    if not thordata_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.thordata_source import ThordataSource

    api_key = get_secret("sources.thordata.api_key", config=cfg) or ""
    queries = thordata_cfg.get("queries", [])
    if not api_key or not queries:
        return []
    source = ThordataSource(api_key, max_age_days=thordata_cfg.get("max_age_days", 3))
    return source.fetch_jobs(queries)


def _fetch_dataforseo_for_benchmark(cfg: dict) -> list[Job]:
    dfse_cfg = cfg.get("sources", {}).get("dataforseo", {})
    if not dfse_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.dataforseo_source import DataForSEOSource

    api_key = get_secret("sources.dataforseo.api_key", config=cfg) or ""
    queries = dfse_cfg.get("queries", [])
    if not api_key or not queries:
        return []
    source = DataForSEOSource(
        api_key,
        max_age_days=dfse_cfg.get("max_age_days", 7),
        depth=dfse_cfg.get("depth", 200),
        priority=dfse_cfg.get("priority", 1),
        poll_interval_seconds=dfse_cfg.get("poll_interval_seconds", 30),
        poll_timeout_seconds=dfse_cfg.get("poll_timeout_seconds", 360),
    )
    return source.fetch_jobs(queries)


def _portal_keywords(cfg: dict, target_titles: list[str]) -> list[str]:
    """Pick the keyword list for free-portal fetchers.

    Prefer the user's explicit ``sources.portal_search.keywords``; fall back
    to ``target_titles`` so the benchmark still has something to ask the
    portals about on a fresh install where portal_search is disabled.
    """
    portal_cfg = cfg.get("sources", {}).get("portal_search", {})
    keywords = portal_cfg.get("keywords") or []
    return keywords if keywords else list(target_titles)


def _fetch_portal_remoteok(cfg: dict, target_titles: list[str]) -> list[Job]:
    from job_finder.sources.portal_search_source import _fetch_remoteok

    return _fetch_remoteok(_portal_keywords(cfg, target_titles))


def _fetch_portal_remotive(cfg: dict, target_titles: list[str]) -> list[Job]:
    from job_finder.sources.portal_search_source import _fetch_remotive

    return _fetch_remotive(_portal_keywords(cfg, target_titles))


def _fetch_portal_himalayas(cfg: dict, target_titles: list[str]) -> list[Job]:
    from job_finder.sources.portal_search_source import _fetch_himalayas

    return _fetch_himalayas(_portal_keywords(cfg, target_titles))


# ---------------------------------------------------------------------------
# Stage-2 portal adapters. Each respects the per-portal ``enabled`` flag so
# the report row is meaningful: a zero count for an enabled portal points at
# a real-yield problem, whereas a disabled portal is simply not measured.
# Adapters that need credentials route them through ``get_secret()`` to honor
# the same env → keyring → config precedence as the live ingestion path.
# ---------------------------------------------------------------------------


def _fetch_portal_jobicy(cfg: dict, target_titles: list[str]) -> list[Job]:
    portal_cfg = cfg.get("sources", {}).get("portal_search", {}).get("jobicy", {})
    if not portal_cfg.get("enabled", False):
        return []
    from job_finder.sources.portal_search_source import _fetch_jobicy

    return _fetch_jobicy(_portal_keywords(cfg, target_titles))


def _fetch_portal_yc_workatastartup(cfg: dict, target_titles: list[str]) -> list[Job]:
    portal_cfg = cfg.get("sources", {}).get("portal_search", {}).get("yc_workatastartup", {})
    if not portal_cfg.get("enabled", False):
        return []
    from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

    return _fetch_yc_workatastartup(_portal_keywords(cfg, target_titles))


def _fetch_portal_usajobs(cfg: dict, target_titles: list[str]) -> list[Job]:
    portal_cfg = cfg.get("sources", {}).get("portal_search", {}).get("usajobs", {})
    if not portal_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.portal_search_source import _fetch_usajobs

    user_agent_email = get_secret(
        "sources.portal_search.usajobs.user_agent_email", config=cfg
    ) or portal_cfg.get("user_agent_email", "") or ""
    authorization_key = get_secret(
        "sources.portal_search.usajobs.authorization_key", config=cfg
    ) or portal_cfg.get("authorization_key", "") or ""
    return _fetch_usajobs(
        _portal_keywords(cfg, target_titles),
        user_agent_email=user_agent_email,
        authorization_key=authorization_key,
    )


def _fetch_portal_adzuna(cfg: dict, target_titles: list[str]) -> list[Job]:
    portal_cfg = cfg.get("sources", {}).get("portal_search", {}).get("adzuna", {})
    if not portal_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.portal_search_source import _fetch_adzuna

    app_id = get_secret(
        "sources.portal_search.adzuna.app_id", config=cfg
    ) or portal_cfg.get("app_id", "") or ""
    app_key = get_secret(
        "sources.portal_search.adzuna.app_key", config=cfg
    ) or portal_cfg.get("app_key", "") or ""
    return _fetch_adzuna(
        _portal_keywords(cfg, target_titles),
        app_id=app_id,
        app_key=app_key,
        country=portal_cfg.get("country", "us") or "us",
    )


def _fetch_portal_jooble(cfg: dict, target_titles: list[str]) -> list[Job]:
    portal_cfg = cfg.get("sources", {}).get("portal_search", {}).get("jooble", {})
    if not portal_cfg.get("enabled", False):
        return []
    from job_finder.secrets import get_secret
    from job_finder.sources.portal_search_source import _fetch_jooble

    api_key = get_secret(
        "sources.portal_search.jooble.api_key", config=cfg
    ) or portal_cfg.get("api_key", "") or ""
    return _fetch_jooble(_portal_keywords(cfg, target_titles), api_key=api_key)


def _fetch_portal_serp_cse(cfg: dict, target_titles: list[str]) -> list[Job]:
    """Stage 3 — exercise the CSE-backed SERP path through ``fetch_serp_portals``.

    Runs only when the user has configured ``sources.google_cse`` *and* not
    ``sources.dataforseo`` (DataForSEO is preferred when both are set, and is
    already reported under the ``dataforseo`` row). The benchmark report should
    not double-count SERP queries.
    """
    cse_cfg = cfg.get("sources", {}).get("google_cse", {})
    if not cse_cfg.get("enabled", False):
        return []

    # If DataForSEO is enabled, the live ingestion path uses it instead of CSE
    # (per load-bearing decision #2). Skip the CSE benchmark row so we don't
    # report a yield that wouldn't run in production.
    dfse_cfg = cfg.get("sources", {}).get("dataforseo", {})
    if dfse_cfg.get("enabled", False):
        return []

    from job_finder.secrets import get_secret
    from job_finder.sources.google_cse_source import GoogleCSESource
    from job_finder.sources.portal_search_source import fetch_serp_portals

    api_key = get_secret("sources.google_cse.api_key", config=cfg) or ""
    cse_id = get_secret("sources.google_cse.cse_id", config=cfg) or ""
    if not (api_key and cse_id):
        return []

    portal_search_cfg = cfg.get("sources", {}).get("portal_search", {})
    max_queries = portal_search_cfg.get("max_serp_queries", 30)

    backend = GoogleCSESource(api_key=api_key, cse_id=cse_id)
    return fetch_serp_portals(
        _portal_keywords(cfg, target_titles),
        dataforseo_source=None,
        max_queries=max_queries,
        google_cse_source=backend,
    )


# Module-level so tests can replace this with {} to stop live HTTP.
_PORTAL_FETCHERS: dict[str, Callable[[dict, list[str]], list[Job]]] = {
    "portal_remoteok": _fetch_portal_remoteok,
    "portal_remotive": _fetch_portal_remotive,
    "portal_himalayas": _fetch_portal_himalayas,
    "portal_jobicy": _fetch_portal_jobicy,
    "portal_yc_workatastartup": _fetch_portal_yc_workatastartup,
    "portal_usajobs": _fetch_portal_usajobs,
    "portal_adzuna": _fetch_portal_adzuna,
    "portal_jooble": _fetch_portal_jooble,
    "portal_serp_cse": _fetch_portal_serp_cse,
}

# Source-name → keyed-source adapter. Tuples so the table order is stable
# in the markdown output.
_KEYED_SOURCES: tuple[tuple[str, Callable[[dict], list[Job]]], ...] = (
    ("gmail", _fetch_gmail_for_benchmark),
    ("imap", _fetch_imap_for_benchmark),
    ("serpapi", _fetch_serpapi_for_benchmark),
    ("thordata", _fetch_thordata_for_benchmark),
    ("dataforseo", _fetch_dataforseo_for_benchmark),
)

# Which keyed sources are considered "paid" and skipped under --no-paid.
_PAID_SOURCES: frozenset[str] = frozenset({"serpapi", "thordata", "dataforseo"})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser():
    import argparse

    p = argparse.ArgumentParser(
        prog="benchmark_sources",
        description="Per-source effectiveness benchmark for Job Cannon.",
    )
    p.add_argument(
        "--no-paid",
        action="store_true",
        help="Skip serpapi/thordata/dataforseo and SERP-backed portal queries.",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Markdown output file. If omitted, the report is written to stdout.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml. Defaults to the user-data directory resolution.",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Path to jobs.db for novel/overlap math. Defaults to the user-data DB.",
    )
    return p


def _resolve_db_path(cli_db: str | None) -> str:
    """Pick the DB path: --db wins, then env, then user_data_dirs default."""
    import os

    if cli_db:
        return cli_db
    env_path = os.environ.get("JOB_CANNON_DB")
    if env_path:
        return env_path
    from job_finder.web import user_data_dirs

    return str(user_data_dirs.db_path())


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and emit a markdown report.

    Returns:
        0 on success, 1 on bad config / missing target_titles.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_argparser().parse_args(argv)

    from job_finder.config import ConfigError, ConfigNotFoundError, load_config

    try:
        cfg = load_config(args.config) if args.config else load_config()
    except (ConfigNotFoundError, ConfigError, ValueError) as e:
        # ValueError covers validate_required_sections (which currently raises
        # bare ValueError, not ConfigError) so a malformed config exits 1
        # instead of dumping a traceback.
        print(f"error: {e}", flush=True)
        return 1

    target_titles = cfg.get("profile", {}).get("target_titles") or []
    if not target_titles:
        print(
            "error: profile.target_titles is empty — benchmark requires queries to be meaningful.",
            flush=True,
        )
        return 1

    db_path = _resolve_db_path(args.db)
    existing_keys = load_existing_dedup_keys(db_path)

    results: list[SourceResult] = []

    # Keyed sources first (so the report shows the paid lane before the free lane).
    for name, fetcher in _KEYED_SOURCES:
        if args.no_paid and name in _PAID_SOURCES:
            continue
        source_cfg = cfg.get("sources", {}).get(name, {})
        if not source_cfg.get("enabled", False):
            continue
        results.append(
            benchmark_one_source(
                name,
                lambda f=fetcher: f(cfg),
                target_titles=target_titles,
                existing_keys=existing_keys,
            )
        )

    # Free-API portals (always-on, no key required).
    for portal_name, portal_fn in _PORTAL_FETCHERS.items():
        results.append(
            benchmark_one_source(
                portal_name,
                lambda fn=portal_fn: fn(cfg, target_titles),
                target_titles=target_titles,
                existing_keys=existing_keys,
            )
        )

    report = format_markdown_report(
        results,
        target_titles=target_titles,
        existing_count=len(existing_keys),
        no_paid=args.no_paid,
    )

    if args.output:
        from pathlib import Path

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"wrote {args.output} ({len(results)} sources)", flush=True)
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
