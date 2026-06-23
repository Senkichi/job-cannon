"""Thread-safe per-request SQLite connection helper for Flask.

Uses Flask's request context (g object) to maintain one connection per
request/thread. Register close_db with app.teardown_appcontext in create_app().

Usage in Flask app factory:
    from .db_helpers import get_db, close_db
    app.teardown_appcontext(close_db)
"""

import copy
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from flask import current_app, g

from job_finder.json_utils import (
    safe_json_load,  # noqa: F401 -- re-exported for backward compatibility
    utc_now_iso,
)

logger = logging.getLogger(__name__)


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Return the per-request SQLite connection, creating it if needed.

    The connection uses sqlite3.Row as its row_factory, enabling column
    access by name (e.g., row["title"]) in addition to index.

    Args:
        db_path: Optional path to the SQLite database file. When None
            (default), reads from ``current_app.config["DB_PATH"]``. Most
            blueprint routes should omit this argument; pass it explicitly
            only when constructing a connection outside the canonical
            request-scoped path (rare).

    Returns:
        An open sqlite3.Connection scoped to the current Flask request context.

    Thread-safety contract:
        check_same_thread=False is intentional — Flask's g object ensures this
        connection is used only within a single request thread. Background jobs
        (APScheduler, stale_detector) MUST create their own sqlite3.connect()
        calls and MUST NOT share or reference g.db across thread boundaries.
        Violating this contract causes silent data corruption under concurrent load.
    """
    if "db" not in g:
        path = db_path if db_path is not None else current_app.config["DB_PATH"]
        g.db = sqlite3.connect(path, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None) -> None:
    """Close the per-request SQLite connection on request teardown.

    Registered as an app teardown handler so Flask calls it automatically
    at the end of each request context.

    Args:
        e: Optional exception from the request context (unused, required by Flask).
    """
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def standalone_connection(db_path: str):
    """Context manager for background/CLI sqlite3 connections.

    Sets row_factory=Row and WAL mode. NOT for Flask request handlers (use
    get_db() via g.db instead).

    Usage:
        with standalone_connection(db_path) as conn:
            rows = conn.execute("SELECT ...").fetchall()
            conn.commit()
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL mode ensures concurrent read/write safety for background jobs
    conn.execute("PRAGMA journal_mode=WAL")
    # Busy timeout: wait up to 30s for write locks to clear instead of
    # failing immediately. Prevents "database is locked" when batch scoring
    # threads compete with Flask HTMX polling for write access.
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def get_config_snapshot(app) -> dict:
    """Return a frozen deep copy of JF_CONFIG for use in background threads.

    Background threads (APScheduler jobs) call this at job-start to get an
    immutable snapshot, rather than reading individual keys from the shared
    dict across multiple statements.

    Args:
        app: Flask application instance.

    Returns:
        Deep copy of app.config["JF_CONFIG"], or empty dict if not set.
    """
    return copy.deepcopy(app.config.get("JF_CONFIG", {}))


def refresh_jf_config(app, config: dict) -> None:
    """Atomically refresh the live in-memory config after a config.yaml write.

    Single point of enforcement called by both the Settings save route and the
    onboarding wizard Done step so neither can drift.

    JF_CONFIG is replaced in a single dict-key assignment (atomic under the GIL),
    so a concurrent reader sees either the whole old config or the whole new one
    — never a torn mix of the two. Background threads should still snapshot it
    once (``get_config_snapshot``) at job-start rather than re-reading keys
    across awaits.

    DB_PATH is the process-wide authority for the database location and is fixed
    at ``create_app()`` time; it is deliberately NOT mutated here. A ``db.path``
    that differs from the running DB_PATH means the user is trying to relocate
    the database, which requires a restart — open connections, WAL files, and
    scheduler jobs are all bound to the original path — so we log a warning
    instead of half-applying a live swap (the torn cross-key write this used to
    risk). Both real callers pass the current DB_PATH back in, so this is a
    no-op warning in practice.

    Args:
        app: Flask application instance (concrete object, not a proxy).
        config: The fully-merged config dict that was just written to disk.
    """
    new_db_path = config.get("db", {}).get("path")
    current_db_path = app.config.get("DB_PATH")
    if current_db_path and new_db_path and str(new_db_path) != str(current_db_path):
        logger.warning(
            "config db.path=%r differs from the running DB_PATH=%r; the database "
            "location is fixed for this process. Restart to use the new path.",
            new_db_path,
            current_db_path,
        )
    elif not current_db_path and new_db_path:
        # First-time seed only (create_app always sets DB_PATH, so this is just a
        # defensive path for bare-app callers); never overwrites an existing one.
        app.config["DB_PATH"] = str(new_db_path)
    # Atomic single-key swap — no cross-key torn-read window.
    app.config["JF_CONFIG"] = config


# ---------------------------------------------------------------------------
# Shared HTMX polling helper for batch_score_sessions rows.
#
# Background: sync_status (blueprints/sync.py) and batch_score_status
# (blueprints/batch_scoring.py) both poll the same batch_score_sessions table
# and render either a progress or a done fragment, with an optional 30-minute
# timeout safety net that flips a still-running row to status='error'. The two
# routes drifted (different WHERE clauses, different "session not found" copy,
# different ValueError logging). PollingSessionConfig + render_polling_status
# express the shared spine once; per-route differences (template names, the
# per-fragment context shape, the optional HX-Trigger-After-Settle header) are
# parameterized through the dataclass.
# ---------------------------------------------------------------------------


_POLLING_TERMINAL_STATES: tuple[str, ...] = ("done", "error", "cancelled")

# Heartbeat-staleness threshold (minutes). A non-terminal session that hasn't
# ticked within this window is treated as dead: render_polling_status flips it
# to status='error', and any "is a scan in flight?" lookup (e.g.
# companies._find_running_scan_session) must use the SAME threshold so it never
# re-mounts a session the poller would immediately fail. Single source of truth.
_POLLING_TIMEOUT_MINUTES: int = 30


@dataclass(frozen=True, slots=True)
class PollingSessionConfig:
    """Per-route knobs for ``render_polling_status``.

    Args:
        progress_template: Jinja template path for the "still running" fragment.
        done_template: Jinja template path for the terminal/not-found fragment.
        progress_ctx: Callable mapping the ``batch_score_sessions`` row to the
            progress template's render context dict.
        done_ctx: Callable mapping ``(row, status, error_msg)`` to the done
            template's render context dict. ``status`` is the resolved status
            (may be ``"error"`` from a timeout); ``error_msg`` is the message
            string or ``None``.
        not_found_ctx: Render context for the "session not found" path. Used
            when the row lookup misses.
        hx_trigger_after_settle: Optional payload encoded into an
            ``HX-Trigger-After-Settle`` header on terminal/timeout responses.
            ``None`` means no header. The progress fragment never gets the
            header (HTMX polling handles its own re-fetch).
        timeout_minutes: Sessions older than this without reaching a terminal
            state are flipped to ``status='error'``. Defaults to 30.
        session_label: Human-readable label prefixed to log messages.
    """

    progress_template: str
    done_template: str
    progress_ctx: Callable[[sqlite3.Row], dict]
    done_ctx: Callable[[sqlite3.Row, str, str | None], dict]
    not_found_ctx: dict = field(default_factory=dict)
    hx_trigger_after_settle: dict | None = None
    timeout_minutes: int = _POLLING_TIMEOUT_MINUTES
    session_label: str = "session"


def _attach_hx_trigger(rendered_html, trigger_payload: dict | None):
    """Wrap a rendered string in ``make_response`` to add HX-Trigger-After-Settle.

    Returns the rendered string unchanged when ``trigger_payload`` is falsy,
    so callers that never want the header avoid the ``make_response`` round-trip.
    """
    if not trigger_payload:
        return rendered_html
    from flask import make_response

    resp = make_response(rendered_html)
    resp.headers["HX-Trigger-After-Settle"] = json.dumps(trigger_payload)
    return resp


def render_polling_status(
    db_path: str,
    session_id: int,
    cfg: PollingSessionConfig,
):
    """Shared body for HTMX polling routes against ``batch_score_sessions``.

    Looks up the session row in its own ``standalone_connection`` (so HTMX
    polling stays safe outside the per-request ``g.db`` thread). Returns:

    - the **done** template rendered with ``cfg.not_found_ctx`` when no row
      exists for ``session_id``;
    - the **done** template (optionally with HX-Trigger-After-Settle) when the
      session is in a terminal state, or when the timeout safety net just
      flipped it to ``error``;
    - the **progress** template otherwise.

    Args:
        db_path: Path to the SQLite database file.
        session_id: ``batch_score_sessions.id`` to poll.
        cfg: Per-route knobs (templates, context callables, optional HTMX trigger).

    Returns:
        Either a plain rendered template string (Flask treats it as 200/text/html)
        or a ``flask.Response`` with HX-Trigger-After-Settle attached.
    """
    from flask import render_template

    with standalone_connection(db_path) as conn:
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(cfg.done_template, **cfg.not_found_ctx)

    status = session["status"]
    timeout_msg = f"No progress in >{cfg.timeout_minutes} min"

    # Heartbeat-based staleness: a session is "alive" iff it has ticked
    # recently. The bg thread (companies.py:_tick / batch_scoring.py) writes
    # last_tick_at on every progress flush. COALESCE falls back to started_at
    # for pre-m065 rows or any row that hasn't ticked yet -- preserves the
    # legacy "elapsed since start" semantics in that edge case so a session
    # that crashes before its first tick still trips the timeout. Once any
    # tick has landed, only tick freshness matters; a multi-hour ATS scan
    # that ticks every ~8s stays alive indefinitely.
    heartbeat_iso = (
        session["last_tick_at"]
        # sqlite3.Row's __contains__ checks values, not keys; .keys() is required.
        if "last_tick_at" in session.keys() and session["last_tick_at"]  # noqa: SIM118
        else session["started_at"]
    )
    if status not in _POLLING_TERMINAL_STATES and heartbeat_iso:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_iso)
            elapsed_min = (datetime.now(UTC).replace(tzinfo=None) - heartbeat).total_seconds() / 60
            if elapsed_min > cfg.timeout_minutes:
                logger.warning(
                    "%s session %s stale: no tick for %.1f minutes",
                    cfg.session_label,
                    session_id,
                    elapsed_min,
                )
                with standalone_connection(db_path) as timeout_conn:
                    timeout_conn.execute(
                        "UPDATE batch_score_sessions SET status='error', "
                        "error_msg=?, finished_at=? "
                        "WHERE id=? AND status NOT IN ('done', 'error', 'cancelled')",
                        (timeout_msg, utc_now_iso(), session_id),
                    )
                    timeout_conn.commit()
                ctx = cfg.done_ctx(session, "error", timeout_msg)
                return _attach_hx_trigger(
                    render_template(cfg.done_template, **ctx),
                    cfg.hx_trigger_after_settle,
                )
        except (ValueError, TypeError):
            logger.debug(
                "%s timeout check failed for session %s",
                cfg.session_label,
                session_id,
                exc_info=True,
            )

    if status in _POLLING_TERMINAL_STATES:
        error_msg = session["error_msg"] if status == "error" else None
        ctx = cfg.done_ctx(session, status, error_msg)
        return _attach_hx_trigger(
            render_template(cfg.done_template, **ctx),
            cfg.hx_trigger_after_settle,
        )

    ctx = cfg.progress_ctx(session)
    return render_template(cfg.progress_template, **ctx)


def reap_orphan_sessions(db_path: str) -> int:
    """Flip non-terminal ``batch_score_sessions`` rows to ``'error'`` at startup.

    Sync / batch-scoring / ATS-scan sessions run in daemon threads that die with
    the process. Any row still at ``status='running'`` (or ``'cancelling'``) when
    a fresh process boots was orphaned by a previous, now-dead process — its
    worker thread no longer exists, so it can never reach a terminal state on its
    own. Left untouched, such a row gets re-mounted as in-flight by progress
    lookups (``companies._find_running_scan_session``), surfacing a phantom scan
    banner that the poller then flips to a confusing "No progress in >N min"
    error on the first poll.

    Reaping here makes the invalid "running-but-dead" state unrepresentable
    across restarts. MUST be called only from the scheduler-owning process
    (after the pidfile lock is acquired), so a live session from a concurrent
    process is never clobbered.

    Returns the number of rows reaped.
    """
    try:
        with standalone_connection(db_path) as conn:
            cursor = conn.execute(
                "UPDATE batch_score_sessions "
                "SET status='error', "
                "    error_msg=COALESCE(error_msg, 'Interrupted by app restart'), "
                "    finished_at=? "
                "WHERE status NOT IN ('done', 'error', 'cancelled')",
                (utc_now_iso(),),
            )
            conn.commit()
            reaped = cursor.rowcount
        if reaped:
            logger.info("Reaped %d orphan session row(s) at startup", reaped)
        return reaped
    except Exception:
        logger.warning("Failed to reap orphan sessions at startup", exc_info=True)
        return 0
