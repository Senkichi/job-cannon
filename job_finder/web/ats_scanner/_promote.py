"""Source-URL based ATS promotion for miss/error/pending companies.

Uses centralized ``reconcile_company_ats`` (.planning/ATS-RECONCILIATION-PLAN Phase B).
Aggregates per-job ``source_urls`` with precedence ranking, verifies with live API
calls, writes audited evidence columns — never trusts URL shape alone.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
"""

import logging

from job_finder.web.ats_identity_reconcile import promote_ats_scheduler_batch

logger = logging.getLogger(__name__)


def promote_ats_from_source_urls(db_path: str, config: dict) -> dict:
    """Backward-compatible facade for nightly scheduler.

    Processes up to ``ats.identity_reconcile.max_companies_per_promote_run``.
    Includes ``pending`` companies with ATS URLs (Phase B backlog drain).

    Thread-safe via internal ``standalone_connection`` per batch iteration.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict (JF_CONFIG snapshot).

    Returns:
        Counts keyed by outcome (``checked``, ``promoted``, failures, skips).
    """
    summary = promote_ats_scheduler_batch(db_path, config)
    logger.info(
        "promote_ats_from_source_urls summary: %s",
        summary,
    )
    return summary
