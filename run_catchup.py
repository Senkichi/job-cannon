"""One-shot catch-up script: runs all missed overnight/morning scheduled jobs."""

import logging
import time
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("catchup")

from job_finder.web import create_app

app = create_app()

def run_job(name, func, *args, **kwargs):
    logger.info("=" * 60)
    logger.info("START: %s", name)
    t0 = time.time()
    try:
        result = func(*args, **kwargs)
        elapsed = round(time.time() - t0, 1)
        logger.info("DONE:  %s (%ss) -> %s", name, elapsed, result)
        return result
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        logger.error("FAIL:  %s (%ss) -> %s: %s", name, elapsed, type(e).__name__, e)
        return None

with app.app_context():
    from job_finder.web.db_helpers import get_config_snapshot
    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")
    logger.info("DB: %s", db_path)

    # 1. Ingestion pipeline (midnight + 8am runs combined into one)
    from job_finder.web.pipeline_runner import run_ingestion
    run_job("Ingestion pipeline", run_ingestion, db_path, config)

    # 2. Pipeline detection (normally every 30 min)
    from job_finder.web.pipeline_detector import run_pipeline_detection
    run_job("Pipeline detection", run_pipeline_detection, db_path, config)

    # 3. Enrichment backfill
    from job_finder.web.data_enricher import run_enrichment_backfill
    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
    run_job("Enrichment backfill", run_enrichment_backfill, db_path,
            serpapi_key=serpapi_key, config=config, limit=200)

    # 4-6. Unified staleness check (nightly 2am) — replaces the old trio
    # of stale_detection + expiry_check + liveness_check with one run that
    # does batch ATS reconciliation, time-based stale marking, and parallel
    # HTTP cascade.
    from job_finder.web.expiry_checker import run_staleness_check
    run_job("Staleness check", run_staleness_check, db_path, config)

    # 7. ATS source-URL promotion (4:45am)
    from job_finder.web.ats_scanner import promote_ats_from_source_urls
    run_job("ATS source-URL promotion", promote_ats_from_source_urls, db_path, config)

    # 8. Company linkage (5am)
    from job_finder.web.backfill_companies import run_company_linkage
    run_job("Company linkage", run_company_linkage, db_path, config)

    # 9. Homepage discovery (6:30am)
    from job_finder.web.homepage_discoverer import run_homepage_discovery
    run_job("Homepage discovery", run_homepage_discovery, db_path, config)

    # 10. Careers crawl (5am) — can be slow with Playwright
    from job_finder.web.careers_crawler import crawl_careers_batch
    if config.get("careers_crawl", {}).get("enabled", True):
        run_job("Careers crawl", crawl_careers_batch, db_path, config)
    else:
        logger.info("SKIP:  Careers crawl (disabled)")

    # 11. ATS scan (7am)
    from job_finder.web.ats_scanner import run_ats_scan
    if config.get("ats", {}).get("scan_enabled", True):
        run_job("ATS scan", run_ats_scan, db_path, config)
    else:
        logger.info("SKIP:  ATS scan (disabled)")

    # 12. ATS slug probe (7:30am)
    from job_finder.web.ats_scanner import probe_ats_slugs
    run_job("ATS slug probe", probe_ats_slugs, db_path, config)

    # 13. Agentic backfill (3:30am — Ollama/qwen2.5)
    from job_finder.web.agentic_enricher import run_agentic_backfill
    run_job("Agentic backfill", run_agentic_backfill, db_path, config)

    # 14. Drive feedback poll (every 30 min)
    from job_finder.web.resume_feedback import run_drive_feedback_poll
    run_job("Drive feedback poll", run_drive_feedback_poll, db_path, config)

    # 15. Health heartbeat (6am)
    # Just log checks inline
    from job_finder.web.db_helpers import standalone_connection
    logger.info("=" * 60)
    logger.info("START: Health heartbeat")
    issues = []
    try:
        with standalone_connection(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(occurred_at) FROM user_activity "
                "WHERE action IN ('scheduled_sync', 'sync') "
                "AND occurred_at >= datetime('now', '-14 hours')"
            ).fetchone()
            if not row[0]:
                issues.append("No ingestion in last 14h (expected — we just ran catch-up)")
            row = conn.execute(
                "SELECT MAX(occurred_at) FROM user_activity "
                "WHERE action = 'scheduled_staleness' "
                "AND occurred_at >= datetime('now', '-26 hours')"
            ).fetchone()
            if not row[0]:
                issues.append("Stale detection not logged in last 26h")
    except Exception as e:
        issues.append(f"Health check error: {e}")
    if issues:
        logger.warning("HEALTH: %s", "; ".join(issues))
    else:
        logger.info("HEALTH: All nominal")

    logger.info("=" * 60)
    logger.info("CATCH-UP COMPLETE")
