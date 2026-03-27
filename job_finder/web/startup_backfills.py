"""One-time startup backfills that run in background threads.

These are operational backfills that spawn daemon threads and may call external
APIs (Anthropic, SerpAPI). Separated from db_migrate.py because they have
fundamentally different failure modes and lifecycle characteristics:

- Schema migrations (db_migrate.py): deterministic, version-gated, must succeed
- Startup backfills (this module): best-effort, idempotent, non-fatal on failure

Both modules are called from the app factory (web/__init__.py) in sequence:
first schema migrations, then startup backfills.
"""

from job_finder.web.db_helpers import standalone_connection


def run_description_reformat_once(db_path: str, config: dict) -> None:
    """Start a daemon thread to reformat all job descriptions once (TESTING-guarded).

    Runs run_description_reformat_pass in a background daemon thread after Migration 6.
    Gated by description_reformatted column existence (safe if migration hasn't run).
    Skipped when TESTING=True (per architectural decision to prevent Windows sqlite3
    file lock issues during pytest teardown).

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (reads TESTING and anthropic API key).
    """
    import logging
    logger = logging.getLogger(__name__)

    # Skip in test mode
    if config.get("TESTING"):
        return

    # Key is injected by anthropic-telemetry from ~/.anthropic-telemetry/config.toml.
    # No need to check os.environ for ANTHROPIC_API_KEY.
    try:
        import anthropic

        import threading
        from job_finder.web.description_reformatter import run_description_reformat_pass

        def _run():
            try:
                count = run_description_reformat_pass(db_path, config=config)
                if count > 0:
                    logger.info("Description reformat pass complete: reformatted %d jobs", count)
            except Exception as e:
                logger.warning("Description reformat pass failed (non-fatal): %s", e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        logger.debug("Description reformat pass started in background thread")

    except ImportError:
        logger.debug("anthropic not installed — skipping description reformat pass")
    except Exception as e:
        logger.warning("Failed to start description reformat pass: %s", e)


def run_data_backfills_once(db_path: str, config: dict) -> None:
    """Run one-time data backfills in a background thread.

    Three backfills guarded by a single sentinel ('backfill_v1' in merge_log):
    1. locations_raw: populate from existing location column
    2. posted_date: approximate from first_seen for email-sourced jobs
    3. SerpAPI enrichment: fill jd_full/salary for jobs missing descriptions

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict.
    """
    import logging
    logger = logging.getLogger(__name__)

    if config.get("TESTING"):
        return

    import threading

    def _run():
        try:
            with standalone_connection(db_path) as conn:
                # Check sentinel — skip if already ran
                sentinel = conn.execute(
                    "SELECT id FROM merge_log WHERE merge_source = 'backfill_v1' LIMIT 1"
                ).fetchone()
                if sentinel is not None:
                    return

                logger.info("Running one-time data backfills...")

                # 1. Backfill locations_raw from location column
                updated = conn.execute(
                    "UPDATE jobs SET locations_raw = json_array(location) "
                    "WHERE locations_raw IS NULL AND location IS NOT NULL"
                ).rowcount
                conn.commit()
                if updated:
                    logger.info("Backfill locations_raw: %d jobs updated", updated)

                # 2. Backfill posted_date from first_seen
                updated = conn.execute(
                    "UPDATE jobs SET posted_date = first_seen "
                    "WHERE posted_date IS NULL AND first_seen IS NOT NULL"
                ).rowcount
                conn.commit()
                if updated:
                    logger.info("Backfill posted_date: %d jobs updated", updated)

                # 3. SerpAPI enrichment for jobs missing jd_full
                serpapi_key = (
                    config.get("sources", {}).get("serpapi", {}).get("api_key")
                )
                if serpapi_key:
                    try:
                        from job_finder.web.data_enricher import run_enrichment_backfill
                        count = run_enrichment_backfill(db_path, serpapi_key, config=config, limit=100)
                        if count:
                            logger.info("Enrichment backfill: %d jobs enriched", count)
                    except Exception as e:
                        logger.warning("Enrichment backfill failed (non-fatal): %s", e)
                else:
                    logger.debug("No SerpAPI key — skipping enrichment backfill")

                # Insert sentinel
                from datetime import datetime, timezone
                conn.execute(
                    "INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at) "
                    "VALUES ('__sentinel__', '__sentinel__', 'backfill_v1', ?)",
                    (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),),
                )
                conn.commit()
                logger.info("Data backfills complete.")

        except Exception as e:
            logger.warning("Data backfills failed (non-fatal): %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.debug("Data backfill thread started")
