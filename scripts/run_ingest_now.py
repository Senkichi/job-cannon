"""One-shot: run just the ingestion pipeline (skip all other scheduled jobs).

Used to manually prove v3.0 schema changes work on a real pipeline run without
waiting for the next scheduled firing.
"""

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ingest_now")

from job_finder.config import load_config
from job_finder.web import create_app

# 2026-05-17 hotfix Fix 7: set TESTING=True so create_app's scheduler-skip
# guard fires. This script is a one-shot ingest — it has no reason to
# start a scheduler, and starting one races the live Flask app for the
# pidfile (which the portalocker rewrite from Fix 6 will now reject
# loudly instead of silently overwriting).
cfg = load_config()
cfg["TESTING"] = True
app = create_app(config=cfg)

with app.app_context():
    from job_finder.web.db_helpers import get_config_snapshot
    from job_finder.web.pipeline_runner import run_ingestion

    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")
    logger.info("DB: %s", db_path)
    logger.info("=" * 60)
    logger.info("START: Ingestion pipeline")

    t0 = time.time()
    try:
        summary = run_ingestion(db_path, config)
        elapsed = round(time.time() - t0, 1)
        logger.info("=" * 60)
        logger.info("DONE (%ss)", elapsed)
        for k, v in summary.items():
            logger.info("  %s = %s", k, v)
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        logger.exception("FAIL (%ss): %s", elapsed, exc)
        sys.exit(1)
