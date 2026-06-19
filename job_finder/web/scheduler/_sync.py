"""Manual ``Sync Now`` handler for the sync blueprint.

Distinct from the scheduled-ingestion path (registered by
``register_ingestion`` in ``_jobs.py``). The sync route hits this
synchronously in the request thread and returns a summary the dashboard
can render. Eager imports of ``run_ingestion`` and
``run_pipeline_detection`` are intentional -- there is no scheduler-setup
window to defer them away from.
"""

import logging

from job_finder.web.db_helpers import get_config_snapshot

logger = logging.getLogger(__name__)


def run_sync_now(app) -> dict:
    """Trigger an immediate ingestion run (for the Sync Now button).

    Runs synchronously in the current thread. Returns the ingestion summary.
    If the pipeline fails, returns an error summary dict.

    Args:
        app: Flask application instance.

    Returns:
        Summary dict from run_ingestion, or an error dict if ingestion failed.
    """
    from job_finder.web.pipeline_runner import run_ingestion

    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")

    try:
        summary = run_ingestion(db_path, config, score=False)
        logger.info("Manual sync triggered: %d new jobs", summary.get("jobs_new", 0))
    except Exception as e:
        logger.error("Manual sync failed: %s", e)
        return {
            "gmail_fetched": 0,
            "gmail_errors": [str(e)],
            "imap_fetched": 0,
            "imap_errors": [],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "dataforseo_fetched": 0,
            "dataforseo_errors": [],
            "portal_search_fetched": 0,
            "portal_search_errors": [],
            "jobs_new": 0,
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.0,
            "error": str(e),
        }

    # Run pipeline detection after ingestion (non-blocking on failure)
    try:
        from job_finder.web.pipeline_detector import run_pipeline_detection

        detection_result = run_pipeline_detection(db_path, config)
        summary["detection_auto_updated"] = detection_result.get("auto_updated", 0)
        summary["detection_queued"] = detection_result.get("queued", 0)
        logger.info(
            "Manual sync detection: %d auto-updated, %d queued",
            summary["detection_auto_updated"],
            summary["detection_queued"],
        )
    except Exception as e:
        logger.error("Manual sync pipeline detection failed: %s", e)
        summary["detection_auto_updated"] = 0
        summary["detection_queued"] = 0

    return summary
