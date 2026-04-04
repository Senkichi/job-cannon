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
from flask import Flask, redirect, url_for

load_dotenv()

from job_finder.config import (
    DEFAULT_HAIKU_THRESHOLD,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    DEFAULT_MODEL_HAIKU,
    DEFAULT_MODEL_SONNET,
    DEFAULT_MONTHLY_BUDGET_USD,
    DEFAULT_MULTI_VERSION_THRESHOLD,
    load_config,
)
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

    # Root logger defaults to WARNING, blocking INFO messages before they reach handlers.
    # Set to DEBUG so handler-level filtering (INFO) controls what lands in the file.
    root_logger.setLevel(logging.DEBUG)

    os.makedirs("logs", exist_ok=True)

    file_handler = RotatingFileHandler(
        "logs/app.log",
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


logger = logging.getLogger(__name__)


def create_app(config_path: str = "config.yaml", *, config: dict = None) -> Flask:
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
        cfg = load_config(config_path)
    else:
        cfg = config

    app.config["JF_CONFIG"] = cfg
    app.config["DB_PATH"] = cfg.get("db", {}).get("path", "jobs.db")
    # NOTE: Ephemeral dev-only behavior — a random key is generated on each startup
    # when FLASK_SECRET_KEY is not set in the environment. This intentionally breaks
    # session persistence across restarts (acceptable for a single-user local app).
    # Set FLASK_SECRET_KEY in .env for persistent sessions.
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

    # Activate cross-project API telemetry, budget enforcement, and key injection.
    # Replaces the old JF_ANTHROPIC_API_KEY → ANTHROPIC_API_KEY env var promotion.
    # Key now sourced from ~/.anthropic-telemetry/config.toml (never in os.environ).
    try:
        import anthropic_telemetry
        anthropic_telemetry.activate("job-cannon")
    except ImportError:
        # Package not installed — silently skip; telemetry is optional.
        pass
    except Exception as _e:
        # Package installed but activate() failed (bad config, network error, etc.).
        # Log as warning so the failure is visible without crashing app startup.
        logging.getLogger(__name__).warning("anthropic-telemetry activate() failed: %s", _e)

    # --- Database setup ---
    run_migrations(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)

    # --- One-time background passes (TESTING-guarded) ---
    # Runs after migration so all columns exist.
    # Skipped when config has TESTING key OR when running under pytest (sys.modules check).
    # This prevents Windows sqlite3 file lock issues during pytest teardown.
    _is_testing = cfg.get("TESTING") or "pytest" in sys.modules

    if not _is_testing:
        # --- File logging (skipped in test mode to avoid writing logs/app.log during pytest) ---
        _setup_file_logging()

        from job_finder.web.startup_backfills import (
            run_description_reformat_once,
            run_data_backfills_once,
        )
        run_description_reformat_once(app.config["DB_PATH"], cfg)
        run_data_backfills_once(app.config["DB_PATH"], cfg)

    # --- Jinja2 globals: centralized config defaults ---
    app.jinja_env.globals["DEFAULT_HAIKU_THRESHOLD"] = DEFAULT_HAIKU_THRESHOLD
    app.jinja_env.globals["DEFAULT_MONTHLY_BUDGET_USD"] = DEFAULT_MONTHLY_BUDGET_USD
    app.jinja_env.globals["DEFAULT_MIN_SCORE_THRESHOLD"] = DEFAULT_MIN_SCORE_THRESHOLD
    app.jinja_env.globals["DEFAULT_MULTI_VERSION_THRESHOLD"] = DEFAULT_MULTI_VERSION_THRESHOLD
    app.jinja_env.globals["DEFAULT_LOOKBACK_DAYS"] = DEFAULT_LOOKBACK_DAYS
    app.jinja_env.globals["DEFAULT_MAX_RESULTS"] = DEFAULT_MAX_RESULTS
    app.jinja_env.globals["DEFAULT_MODEL_HAIKU"] = DEFAULT_MODEL_HAIKU
    app.jinja_env.globals["DEFAULT_MODEL_SONNET"] = DEFAULT_MODEL_SONNET

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
    from job_finder.web.blueprints.companies import companies_bp
    from job_finder.web.blueprints.costs import costs_bp
    from job_finder.web.blueprints.dashboard import dashboard_bp
    from job_finder.web.blueprints.batch_scoring import batch_scoring_bp
    from job_finder.web.blueprints.sync import sync_bp
    from job_finder.web.blueprints.detections import detections_bp
    from job_finder.web.blueprints.feedback import feedback_bp
    from job_finder.web.blueprints.jobs import jobs_bp
    from job_finder.web.blueprints.pipeline import pipeline_bp
    from job_finder.web.blueprints.profile import profile_bp
    from job_finder.web.blueprints.resume import resume_bp
    from job_finder.web.blueprints.guidelines import guidelines_bp
    from job_finder.web.blueprints.resume_review import resume_review_bp
    from job_finder.web.blueprints.profile_recommendations import profile_recs_bp
    from job_finder.web.blueprints.settings import settings_bp

    # companies_bp, resume_bp, feedback_bp, costs_bp registered BEFORE jobs_bp (catch-all route) to prevent route shadowing
    app.register_blueprint(companies_bp)
    app.register_blueprint(resume_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(costs_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(batch_scoring_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(detections_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(resume_review_bp)
    app.register_blueprint(profile_recs_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(guidelines_bp)

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
