"""One-time expiry backfill for all eligible jobs.

Usage:
    uv run --active python scripts/run_expiry_backfill.py

Runs the nightly expiry checker (now unbatched) against all jobs in
discovered/reviewing status that haven't been recently checked.
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from job_finder.config import load_config
    from job_finder.web.expiry_checker import run_expiry_check

    config = load_config()
    db_path = config["db"]["path"]

    logger.info("Starting expiry backfill — db: %s", db_path)
    t0 = time.time()
    result = run_expiry_check(db_path, config)
    elapsed = time.time() - t0

    logger.info(
        "Backfill complete in %.1f minutes: %s",
        elapsed / 60,
        result,
    )


if __name__ == "__main__":
    main()
