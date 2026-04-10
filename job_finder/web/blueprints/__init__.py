"""Shared constants and helpers for all blueprints."""

import threading

from job_finder.constants import PIPELINE_STATUSES, VALID_PIPELINE_STATUSES  # noqa: F401 — re-exported

def trigger_interview_prep_if_applied(
    dedup_key: str,
    new_status: str,
    db_path: str,
    config: dict,
    testing: bool = False,
) -> None:
    """Spawn a background thread to generate interview prep when status moves to 'applied'.

    No-op when new_status != 'applied' or testing=True.
    Skip in TESTING mode to prevent background threads holding Windows file locks.
    """
    if new_status != "applied" or testing:
        return
    from job_finder.web.interview_prep import generate_interview_prep_background
    t = threading.Thread(
        target=generate_interview_prep_background,
        args=(dedup_key, db_path, config),
        daemon=True,
    )
    t.start()
