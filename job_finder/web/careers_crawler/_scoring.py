"""Scoring trigger for newly discovered careers-crawl jobs.

After the orchestrator's per-company tiers have produced a list of new
`dedup_key`s, this module enriches each shell row (`jd_full`, salary,
location) and routes it through the unified v3.0 scorer
(`score_and_persist_job`), then accumulates per-classification
counters on the run summary.

All upstream imports (`scoring_orchestrator`, `model_provider`,
`data_enricher`, `anthropic`) are kept lazy to mirror the original
behavior: graceful degradation when a downstream component is absent
(e.g. a checkout without the `eval` extras), so the crawler still
runs end-to-end and just skips scoring.
"""

from __future__ import annotations

import logging

from job_finder.db import derive_classification
from job_finder.secrets import get_secret
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def _score_new_jobs(
    db_path: str,
    config: dict,
    new_job_keys: list[str],
    summary: dict,
) -> None:
    """Score newly discovered jobs via the unified v3.0 scorer.

    v3.0 (Phase 34 Plan 3 Commit A): routes through score_and_persist_job so the
    `classification` column populates on every scored row; per-classification
    counters replace haiku_scored / sonnet_evaluated.
    """
    try:
        from job_finder.web.scoring_orchestrator import score_and_persist_job
    except ImportError:
        logger.debug("scoring_orchestrator not available — skipping scoring")
        return

    try:
        from job_finder.web.data_enricher import enrich_job
    except ImportError:
        enrich_job = None  # type: ignore[assignment]

    # 2026-05-17 hotfix Fix 5: dropped the tier_has_configured_provider
    # pre-flight check. After Fix 4, ProviderCascadeExhaustedError is the
    # canonical "no provider" signal and is caught by the orchestrator's
    # per-job try/except at job_scorer.py — same posture as
    # ats_scanner/_run.py. Eliminating the asymmetry removes a class of
    # cascade-bypass regressions.

    serpapi_key = get_secret("sources.serpapi.api_key", config=config)

    with standalone_connection(db_path) as conn:
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue

                job_row = dict(row)

                # Enrich BEFORE scoring — careers_crawl produces title+URL only
                # shells, so the scorer would otherwise read an empty description.
                if enrich_job is not None and (
                    not job_row.get("jd_full")
                    or job_row.get("salary_min") is None
                    or not job_row.get("location")
                ):
                    try:
                        enriched = enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "careers_crawl enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

                result = score_and_persist_job(
                    job_row,
                    conn,
                    config,
                )
                if result is None:
                    continue
                summary["scored"] = summary.get("scored", 0) + 1
                if getattr(result, "status", None) != "ok" or result.data is None:
                    continue
                cls = derive_classification(
                    result.data.sub_scores,
                    job_row.get("legitimacy_note"),
                    degenerate=getattr(result.data, "degenerate", False),
                )
                key = f"classified_{cls}"
                summary[key] = summary.get(key, 0) + 1
            except Exception as e:
                logger.warning(
                    "careers_crawl scoring error for '%s': %s",
                    dedup_key,
                    e,
                    exc_info=True,
                )
