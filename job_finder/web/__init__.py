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
    # at INFO. The DDGS object retries across engines internally and we already
    # emit a single WARNING from enrichment_tiers when ALL engines fail
    # ("DDGS: all engines returned empty for query"), so the per-engine chatter
    # is pure noise. WARNING level keeps real DDGS-side warnings visible.
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

    # --- Configuration ---
    if config is None:
        from job_finder.settings import migrate_config_keys

        user_data_dirs.ensure_user_data_dir()
        # Use user-data config path if legacy default string is passed
        if config_path == "config.yaml":
            cfg = load_config(allow_missing=True)
        else:
            migrate_config_keys(config_path)
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

    # --- One-time background passes (TESTING-guarded) ---
    # Runs after migration so all columns exist.
    # Skipped when config has TESTING key OR when running under pytest (sys.modules check).
    # This prevents Windows sqlite3 file lock issues during pytest teardown.
    _is_testing = cfg.get("TESTING") or "pytest" in sys.modules

    # Diagnostic / operational escape hatch: skip the long-running startup
    # backfill threads (description reformat + data backfill) without entering
    # full TESTING mode. Useful when manually monitoring scheduled jobs because
    # the description reformat daemon competes for the SQLite write lock and
    # can stall short cron jobs (registry_hygiene etc.) under WAL contention.
    _skip_backfills = bool(os.environ.get("JOB_CANNON_SKIP_STARTUP_BACKFILLS"))

    if not _is_testing:
        # --- File logging (skipped in test mode to avoid writing logs/app.log during pytest) ---
        _setup_file_logging()

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

    # --- Blueprint registration ---
    from job_finder.web.blueprints.admin import admin_bp
    from job_finder.web.blueprints.batch_scoring import batch_scoring_bp
    from job_finder.web.blueprints.companies import companies_bp
    from job_finder.web.blueprints.costs import costs_bp
    from job_finder.web.blueprints.dashboard import dashboard_bp
    from job_finder.web.blueprints.detections import detections_bp
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

    # --- Root redirect: / -> /jobs (Job Board is the default landing page) ---
    @app.route("/")
    def index():
        return redirect(url_for("jobs.index"))

    # --- Background scheduler ---
    # Start AFTER blueprints are registered so the scheduler job can import from
    # the web package without circular imports. Skipped when TESTING=True.
    from job_finder.web.scheduler import init_scheduler

    init_scheduler(app)

    return app
