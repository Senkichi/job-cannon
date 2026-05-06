"""Closure factories for scheduled jobs.

Reduces per-job boilerplate. Two factories:
  - ``_make_simple_job``: config + db_path + try/except wrapper.
  - ``_make_tracked_job``: timing + user_activity log + status classification.

Both produce zero-arg wrappers suitable for ``scheduler.add_job``. The
double-indirection in ``import_func`` is intentional: it defers the import
of heavy job modules until the job actually runs in the background thread,
rather than at scheduler-setup time inside ``init_scheduler``.
"""

import logging

from job_finder.web.db_helpers import get_config_snapshot

logger = logging.getLogger(__name__)


def _make_simple_job(app, name, import_func):
    """Factory for scheduler jobs that need only config + db_path + try/except.

    Args:
        app: Flask application instance.
        name: Human-readable job name for log messages.
        import_func: No-arg callable that returns the job function.
            Called lazily inside the closure to defer imports.
            The returned function must accept (db_path, config).
    """

    def wrapper():
        with app.app_context():
            config = get_config_snapshot(app)
            db_path = app.config.get("DB_PATH", "jobs.db")
            try:
                result = import_func()(db_path, config)
                logger.info("%s: %s", name, result)
            except Exception as e:
                logger.error("%s failed: %s", name, e)

    return wrapper


def _make_tracked_job(app, name, import_func, import_action, extract_metadata, *, guard=None):
    """Factory for scheduler jobs with timing and activity logging.

    Returns a zero-arg ``wrapper`` closure suitable for ``scheduler.add_job``.

    Args:
        app: Flask application instance.
        name: Human-readable job name for log messages.
        import_func: No-arg callable that returns the job function.
            Called lazily inside the closure to defer imports.
            The returned function must accept (db_path, config).
        import_action: No-arg callable that returns the activity action constant.
            Also called lazily to defer activity_tracker imports.
        extract_metadata: Callable(result) -> dict of metadata fields for
            the success activity log entry. duration_seconds and status are
            added automatically.
        guard: Optional callable(config) -> bool. If provided and returns
            False, the job exits early without running.
    """

    def wrapper():
        import time as _time

        with app.app_context():
            from job_finder.web.activity_tracker import log_activity

            config = get_config_snapshot(app)
            db_path = app.config.get("DB_PATH", "jobs.db")
            action = import_action()

            if guard is not None and not guard(config):
                return

            t0 = _time.time()
            try:
                result = import_func()(db_path, config)
                logger.info("%s: %s", name, result)
                metadata = extract_metadata(result)
                metadata["duration_seconds"] = round(_time.time() - t0, 2)
                # status="degraded" when extract_metadata surfaces a non-empty
                # errors list (e.g., pipeline_detection skipped because Gmail
                # auth failed). Truthful status lets the dashboard distinguish
                # "ran clean" from "ran but produced nothing useful".
                metadata["status"] = "degraded" if metadata.get("errors") else "success"
                log_activity(db_path, action, metadata=metadata)
            except Exception as e:
                logger.error("%s failed: %s", name, e)
                log_activity(
                    db_path,
                    action,
                    metadata={
                        "status": "failed",
                        "error": type(e).__name__,
                        "duration_seconds": round(_time.time() - t0, 2),
                    },
                )

    return wrapper
