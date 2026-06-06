"""Flask web application factory for job-finder.

Usage:
    from job_finder.web import create_app
    app = create_app()           # uses config.yaml
    app = create_app(config=d)   # pass config dict directly (tests)
"""

import json
import logging
import os
import secrets
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from flask import Flask, g, redirect, request, url_for

load_dotenv()

from job_finder.config import (
    DEFAULT_CANDIDATE_SCORE_THRESHOLD,
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    load_config,
)
from job_finder.web import user_data_dirs
from job_finder.web.db_helpers import close_db
from job_finder.web.db_migrate import run_migrations
from job_finder.web.description_formatter import format_description_filter


def _setup_file_logging() -> None:
    """Attach RotatingFileHandler to root logger if not already attached.

    Idempotency guard: checks root logger handlers for existing RotatingFileHandler
    before adding a new one. Safe for multiple create_app() calls in tests.
    """
    root_logger = logging.getLogger()

    # Guard: skip if a RotatingFileHandler is already attached
    if any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        return

    log_file = user_data_dirs.logs_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Ensure root logger level allows INFO messages to reach the handler.
    # Without this, the default WARNING level filters them before the handler.
    if root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    # Suppress noisy transitive dependency loggers at the source.
    # primp is a Rust HTTP client pulled in via ddgs; it logs every request at INFO.
    logging.getLogger("primp").setLevel(logging.WARNING)
    # ddgs logs every per-engine fallback failure ("Error in engine yahoo: ...")
    # at INFO. The DDGS object retries across engines internally and our own
    # callers emit a single INFO from enrichment_tiers / agentic_enricher when
    # ALL engines fail ("DDGS: all engines returned empty for query"), so the
    # per-engine chatter is pure noise. WARNING level keeps real DDGS-side
    # warnings visible.
    logging.getLogger("ddgs").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def create_app(config_path: str = "config.yaml", config: dict | None = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        config_path: Path to config.yaml (used when config is None).
        config: Config dict to use directly (takes priority over config_path).
                Useful for testing -- pass a dict with a temp DB path to avoid
                reading config.yaml.

    Returns:
        Configured Flask application instance.
    """
    app = Flask(
        __name__,
        template_folder="templates",
    )

    # Single point of enforcement: the user-data root must exist before any
    # code path touches it (config_path / db_path / logs_path). Previously
    # this was only called on the `config is None` branch, so __main__.py
    # (which pre-loads config and passes it in) would skip directory creation
    # and crash with sqlite3.OperationalError on a fresh macOS install
    # (~/Library/Application Support/JobCannon doesn't exist by default).
    # See UAT 2026-05-21 finding F1.
    user_data_dirs.ensure_user_data_dir()

    # Stamp the app-start wall-clock time for the /__jc_health endpoint.
    # Stored as a naive UTC ISO string (Store-UTC-render-local invariant).
    app.config["_JF_START_TIME_UTC"] = datetime.utcnow().isoformat() + "Z"

    # --- Configuration ---
    if config is None:
        # Use user-data config path if legacy default string is passed
        if config_path == "config.yaml":
            cfg = load_config(allow_missing=True)
        else:
            cfg = load_config(config_path, allow_missing=True)
    else:
        cfg = config

    app.config["JF_CONFIG"] = cfg
    # DB path: explicit config wins, otherwise use user_data_dirs.db_path()
    explicit_db_path = cfg.get("db", {}).get("path")
    if explicit_db_path:
        app.config["DB_PATH"] = explicit_db_path
    else:
        app.config["DB_PATH"] = str(user_data_dirs.db_path())
    if "TESTING" in cfg:
        app.config["TESTING"] = cfg["TESTING"]
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

    # --- Database setup ---
    # Pass user_data_root for default runtime path, preserve test behavior for explicit config
    if explicit_db_path:
        run_migrations(app.config["DB_PATH"])
    else:
        run_migrations(app.config["DB_PATH"], user_data_root=str(user_data_dirs.user_data_root()))
    app.teardown_appcontext(close_db)

    # --- Test isolation: pre-seed onboarding_complete so the @before_request
    # gate doesn't 302 every route to /onboarding/welcome in CI environments
    # (where _legacy_install_detected() returns False because config.yaml /
    # experience_profile.json don't exist on a fresh checkout). Many test
    # fixtures bypass conftest's shared `app` fixture and call create_app()
    # directly; centralizing the seed here avoids requiring every fixture to
    # remember to seed. Tests that exercise the redirect (test_onboarding_gate)
    # UPDATE the row back to 0 in their own fixture (app_unconfigured).
    if cfg.get("TESTING") or "pytest" in sys.modules:
        import sqlite3 as _seed_sqlite3

        _seed_conn = _seed_sqlite3.connect(app.config["DB_PATH"])
        try:
            _seed_conn.execute(
                "INSERT OR IGNORE INTO onboarding_state (id, onboarding_complete) VALUES (1, 1)"
            )
            _seed_conn.commit()
        finally:
            _seed_conn.close()

    # --- One-time background passes (TESTING-guarded) ---
    # Runs after migration so all columns exist.
    # Skipped when config has TESTING key OR when running under pytest (sys.modules check).
    # This prevents Windows sqlite3 file lock issues during pytest teardown.
    # SKIP_SCHEDULER also trips this gate: a secondary app instance (e.g.,
    # scripts/run_overnight.py) needs to skip startup file logging + keyring
    # probe + cache passes, but must NOT propagate TESTING into job functions
    # that check it (careers_crawl, ats_scan, ats_slug_probe, ats_identity_reconcile).
    _is_testing = cfg.get("TESTING") or cfg.get("SKIP_SCHEDULER") or "pytest" in sys.modules

    # Diagnostic / operational escape hatch: skip the long-running startup
    # backfill threads (description reformat + data backfill) without entering
    # full TESTING mode. Useful when manually monitoring scheduled jobs because
    # the description reformat daemon competes for the SQLite write lock and
    # can stall short cron jobs (registry_hygiene etc.) under WAL contention.
    _skip_backfills = bool(os.environ.get("JOB_CANNON_SKIP_STARTUP_BACKFILLS"))

    if not _is_testing:
        # --- File logging (skipped in test mode to avoid writing logs/app.log during pytest) ---
        _setup_file_logging()

        # --- Keyring backend probe (Item 3, KEYRING-v5.1) ---
        # Runs after file logging so the NoKeyringError warning lands in app.log.
        # On success: subsequent get_secret() calls use the OS keyring as step 2
        # of the precedence stack. On failure (headless Linux without D-Bus, etc.):
        # step 2 is skipped and config.yaml plaintext fallback handles everything.
        from job_finder.secrets import probe_keyring_backend

        probe_keyring_backend()

        # Warn loudly if the env var is unset and a jobs.db exists at cwd that the
        # app is about to ignore. Targets the failure mode where a developer's
        # persisted JOB_CANNON_USER_DATA_DIR is missing in a new shell and the app
        # silently starts a fresh onboarding flow at platformdirs.
        user_data_dirs.warn_if_data_split()

        if not _skip_backfills:
            from job_finder.web.startup_backfills import (
                run_data_backfills_once,
                run_description_reformat_once,
            )

            run_description_reformat_once(app.config["DB_PATH"], cfg)
            run_data_backfills_once(app.config["DB_PATH"], cfg)

    # --- Jinja2 globals: centralized config defaults ---
    app.jinja_env.globals["DEFAULT_CANDIDATE_SCORE_THRESHOLD"] = DEFAULT_CANDIDATE_SCORE_THRESHOLD
    app.jinja_env.globals["DEFAULT_MIN_SCORE_THRESHOLD"] = DEFAULT_MIN_SCORE_THRESHOLD
    app.jinja_env.globals["DEFAULT_LOOKBACK_DAYS"] = DEFAULT_LOOKBACK_DAYS
    app.jinja_env.globals["DEFAULT_MAX_RESULTS"] = DEFAULT_MAX_RESULTS
    app.jinja_env.globals["DEFAULT_DAILY_BUDGET_USD"] = DEFAULT_DAILY_BUDGET_USD

    # --- Custom Jinja2 filters ---
    @app.template_filter("from_json")
    def from_json_filter(value):
        """Parse a JSON string into a Python object for use in templates."""
        if not value:
            return []
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []

    @app.template_filter("urlencode")
    def urlencode_filter(value):
        """URL-encode a string (for embedding dedup_keys in URLs)."""
        from urllib.parse import quote

        return quote(str(value), safe="")

    app.jinja_env.filters["format_description"] = format_description_filter

    @app.template_filter("format_canonical_location")
    def format_canonical_location_filter(value, max_entries: int = 3) -> str:
        """Render ``locations_structured`` (JSON or list[JobLocation]) as text.

        Accepts:
          - a JSON string from ``jobs.locations_structured`` (NULL → ``""``)
          - a single ``JobLocation`` dataclass
          - a ``list[JobLocation]``
          - a list of plain dicts (after JSON deserialization)

        Returns a comma-separated string of entries, capped at ``max_entries``;
        any overflow is summarized as ``+N more``. Each entry is rendered in
        ``"City, Region · Country · Workplace"`` order, omitting absent
        fields and the workplace suffix when UNSPECIFIED.

        Falsy / unparseable input → empty string. The caller is expected
        to fall back to the legacy ``location`` column when this returns
        empty.
        """
        if not value:
            return ""
        # Lazy import: avoids a cold-path circular when this module is
        # imported before location_canonical's dataclass binds.
        from job_finder.web.location_canonical import JobLocation

        # Coerce input shapes to a list of mapping-like records.
        records: list = []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return ""
            records = parsed if isinstance(parsed, list) else [parsed]
        elif isinstance(value, JobLocation):
            records = [value]
        elif isinstance(value, list):
            records = value
        else:
            return ""

        def _field(rec, key):
            if isinstance(rec, JobLocation):
                return getattr(rec, key, None)
            if isinstance(rec, dict):
                return rec.get(key)
            return None

        rendered: list[str] = []
        for rec in records[:max_entries]:
            city = _field(rec, "city") or ""
            region_code = _field(rec, "region_code") or ""
            country_code = _field(rec, "country_code") or ""
            workplace_type = _field(rec, "workplace_type") or ""

            head_parts = [p for p in (city, region_code) if p]
            head = ", ".join(head_parts)
            tail_parts = []
            if country_code:
                tail_parts.append(country_code)
            if workplace_type and workplace_type != "UNSPECIFIED":
                tail_parts.append(workplace_type.title())

            pieces = [p for p in (head, *tail_parts) if p]
            if pieces:
                rendered.append(" · ".join(pieces))

        if not rendered:
            return ""

        overflow = len(records) - max_entries
        if overflow > 0:
            rendered.append(f"+{overflow} more")
        return ", ".join(rendered)

    # --- Blueprint registration ---
    from job_finder.web.blueprints.admin import admin_bp
    from job_finder.web.blueprints.batch_scoring import batch_scoring_bp
    from job_finder.web.blueprints.companies import companies_bp
    from job_finder.web.blueprints.costs import costs_bp
    from job_finder.web.blueprints.dashboard import dashboard_bp
    from job_finder.web.blueprints.detections import detections_bp
    from job_finder.web.blueprints.events import events_bp
    from job_finder.web.blueprints.jobs import jobs_bp
    from job_finder.web.blueprints.pipeline import pipeline_bp
    from job_finder.web.blueprints.profile import profile_bp
    from job_finder.web.blueprints.settings import settings_bp
    from job_finder.web.blueprints.sync import sync_bp
    from job_finder.web.blueprints.updates import updates_bp

    # companies_bp, costs_bp registered BEFORE jobs_bp (catch-all route) to prevent route shadowing
    app.register_blueprint(companies_bp)
    app.register_blueprint(costs_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(batch_scoring_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(detections_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(updates_bp)
    app.register_blueprint(events_bp)  # SSE live-update stream (/events)

    @app.context_processor
    def _inject_live_updates_flag():
        """Expose LIVE_UPDATES_ENABLED to every template.

        Defaults True (the SSE stream + sse:* triggers render). The e2e harness
        sets it False because a long-lived /events connection never reaches
        Playwright's networkidle and would block the single-threaded test
        server. Live updates are covered by unit tests instead.
        """
        return {"live_updates_enabled": app.config.get("LIVE_UPDATES_ENABLED", True)}

    # --- Update banner context (Phase 43) ---
    @app.before_request
    def _inject_update_banner():
        """Lazy-fetch update check (D-01) + per-request banner context (D-05/D-08b)."""
        # Suppress on the entire /onboarding/* tree (D-05 + D-08b)
        if request.path.startswith("/onboarding/"):
            g.update_banner = None
            return

        # Kick off the background fetch if the 24h window has elapsed (D-01).
        # No-op when TESTING=True or cache is fresh.
        from job_finder.web.update_check import (
            banner_context,
            kick_off_background_check_if_due,
        )

        kick_off_background_check_if_due(app.config)
        g.update_banner = banner_context()

    @app.context_processor
    def _update_banner_ctx():
        return {"update_banner": getattr(g, "update_banner", None)}

    # --- Phase 42: Onboarding wizard blueprint + before_request gate ---
    from job_finder.web.onboarding.blueprint import onboarding_bp
    from job_finder.web.onboarding.state import gate_onboarding

    app.register_blueprint(onboarding_bp)
    app.before_request(gate_onboarding)

    # --- Health / identity endpoint (registered directly on app, not via blueprint) ---
    # This endpoint is the load-bearing identity marker for already-running
    # detection in __main__.py: probe_existing_jc() checks data["app"] == "job-cannon".
    # Registered BEFORE blueprints guard conditions so it is reachable the instant
    # the app object exists. Do NOT move this into a Blueprint — the launcher's
    # HTTP probe must reach it even if some blueprint fails to register.
    @app.route("/__jc_health")
    def __jc_health():
        try:
            from importlib.metadata import version as _pkg_version

            _version = _pkg_version("job-cannon")
        except Exception:
            _version = "0.0.0+dev"
        return (
            {
                "app": "job-cannon",
                "version": _version,
                "pid": os.getpid(),
                "start_time_utc": app.config.get("_JF_START_TIME_UTC", ""),
            },
            200,
        )

    # --- Root redirect: / -> /jobs (Job Board is the default landing page) ---
    @app.route("/")
    def index():
        return redirect(url_for("jobs.index"))

    # --- Favicon: inline SVG so every page stops 404'ing in the browser console ---
    @app.route("/favicon.ico")
    def favicon():
        # Tiny SVG bullseye on transparent ground — matches the "Job Cannon"
        # naming and avoids shipping a binary asset / adding a static dir.
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<circle cx="16" cy="16" r="14" fill="#1e293b"/>'
            '<circle cx="16" cy="16" r="10" fill="none" stroke="#3b82f6" stroke-width="2"/>'
            '<circle cx="16" cy="16" r="4" fill="#3b82f6"/>'
            "</svg>"
        )
        return svg, 200, {"Content-Type": "image/svg+xml"}

    # --- Background scheduler ---
    # Start AFTER blueprints are registered so the scheduler job can import from
    # the web package without circular imports. Skipped when TESTING=True.
    from job_finder.web.scheduler import init_scheduler

    init_scheduler(app)

    return app
