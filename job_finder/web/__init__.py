"""Flask web application factory for job-finder.

Usage:
    from job_finder.web import create_app
    app = create_app()           # uses config.yaml
    app = create_app(config=d)   # pass config dict directly (tests)
"""

import html as _html
import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from flask import Flask, redirect, url_for

load_dotenv()
# Re-export under the standard name so anthropic.Anthropic() finds it.
# The .env file uses JF_ANTHROPIC_API_KEY to prevent Claude Code and other
# tools from detecting and consuming the key (which was costing ~$40/day).
os.environ["ANTHROPIC_API_KEY"] = os.environ.get("JF_ANTHROPIC_API_KEY", "")
from markupsafe import Markup, escape

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


def _setup_file_logging() -> None:
    """Attach RotatingFileHandler to root logger if not already attached.

    Idempotency guard: checks root logger handlers for existing RotatingFileHandler
    before adding a new one. Safe for multiple create_app() calls in tests.
    """
    root_logger = logging.getLogger()

    # Guard: skip if a RotatingFileHandler is already attached
    if any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        return

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


def create_app(config_path: str = "config.yaml", config: dict = None) -> Flask:
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
    app.config["DB_PATH"] = cfg["db"]["path"]
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

    # --- File logging ---
    _setup_file_logging()

    # --- Database setup ---
    run_migrations(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)

    # --- One-time background passes (TESTING-guarded) ---
    # Runs after migration so all columns exist.
    # Skipped when config has TESTING key OR when running under pytest (sys.modules check).
    # This prevents Windows sqlite3 file lock issues during pytest teardown.
    _is_testing = cfg.get("TESTING") or "pytest" in os.sys.modules
    if not _is_testing:
        # --- Startup validation ---
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning(
                "ANTHROPIC_API_KEY is not set. AI scoring and resume generation will not work. "
                "Add it to your .env file: ANTHROPIC_API_KEY=sk-ant-..."
            )
        from job_finder.web.db_migrate import (
            _run_description_reformat_once,
            _run_data_backfills_once,
        )
        _run_description_reformat_once(app.config["DB_PATH"], cfg)
        _run_data_backfills_once(app.config["DB_PATH"], cfg)

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

    # Matches markdown headers (# Title, ## Section) or plain-text section headers
    _md_header_re = re.compile(r'^#{1,3}\s+(.+)$', re.MULTILINE)
    _plain_header_re = re.compile(
        r"^(?:About|Overview|Summary|Responsibilities|Requirements|Qualifications|"
        r"Benefits|What You|Minimum|Preferred|Nice to Have|The Role|Your Role|"
        r"Who You Are|What We|Key |Job |Position |Company |Team |Culture |"
        r"About the |How to |Why |Our |Skills|Experience|Education|Compensation|"
        r"Duties|Description|Location)",
        re.IGNORECASE,
    )
    _bullet_re = re.compile(r'^\s*[-*]\s')
    _html_tag_re = re.compile(r'<[a-zA-Z/][^>]*>')

    def _strip_html_to_text(text):
        """Strip HTML tags from text, preserving structure via newlines and bullet markers.

        Converts block-level closing tags to newlines and <li> to bullet prefixes
        so the plain-text structured renderer can detect headers and bullets.
        """
        # Convert <br> variants to newlines
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        # Convert <li> to bullet prefix for list items
        text = re.sub(r'<li[^>]*>', '- ', text, flags=re.IGNORECASE)
        # Convert closing block-level tags to newlines
        text = re.sub(
            r'</(?:p|div|h[1-6]|li|ul|ol|tr|td|th|table|section|article|'
            r'header|footer|blockquote)\s*>',
            '\n', text, flags=re.IGNORECASE,
        )
        # Strip all remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Decode any remaining entities (e.g. &amp; &nbsp;)
        text = _html.unescape(text)
        # Collapse 3+ newlines to 2
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @app.template_filter("format_description")
    def format_description_filter(value):
        """Format job description text into safe HTML.

        Handles three description formats:
        1. Structured (markdown headers or plain-text section headers with
           paragraphs and bullet lists) -- renders with proper HTML structure.
        2. Legacy pipe-separated -- renders as a bullet list.
        3. Simple text -- renders as a paragraph.
        """
        if not value:
            return ""

        # Decode any HTML entities stored in the DB before rendering.
        # Must happen BEFORE escape() so entities like &amp; become & first,
        # then escape() re-encodes for safe HTML output.
        value = _html.unescape(value)

        # If the unescaped text contains HTML tags (from entity-encoded HTML
        # in the DB like &lt;p&gt;), strip them to plain text while preserving
        # structure. Without this, escape() in the renderer would re-encode
        # the tags back to visible &lt;p&gt; entities.
        if _html_tag_re.search(value):
            value = _strip_html_to_text(value)

        # Legacy pipe-separated format (no newlines, has pipes)
        if '\n' not in value and '|' in value:
            parts = [p.strip() for p in value.split('|') if p.strip()]
            items = ''.join(f'<li class="mb-1">{escape(p)}</li>' for p in parts)
            return Markup(f'<ul class="list-disc list-inside space-y-1">{items}</ul>')

        # Single line, no structure
        if '\n' not in value:
            return Markup(f'<p>{escape(value)}</p>')

        # Multi-line: render line-by-line with structure detection
        return _render_structured_description(value)

    def _is_header(line):
        """Check if a line is a section header (markdown or plain text)."""
        stripped = line.strip()
        if _md_header_re.match(stripped):
            return True
        return bool(_plain_header_re.match(stripped))

    def _header_text(line):
        """Extract display text from a header line (strips markdown #)."""
        stripped = line.strip()
        md = _md_header_re.match(stripped)
        if md:
            return md.group(1).strip()
        return stripped

    def _merge_orphaned_words(lines):
        """Merge single capitalized words with their lowercase continuations.

        Fixes browser-paste artifacts where bold verbs (e.g., <strong>Lead</strong>)
        get captured as separate lines from their sentence continuations.
        """
        merged = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if (stripped and re.match(r'^[A-Z][a-z]+$', stripped)
                    and i + 1 < len(lines)
                    and lines[i + 1].strip()
                    and lines[i + 1].strip()[0].islower()):
                merged.append(f"{stripped} {lines[i + 1].strip()}")
                i += 2
            else:
                merged.append(lines[i])
                i += 1
        return merged

    def _render_structured_description(value):
        """Render a description with headers, paragraphs, and bullet lists."""
        html_parts = []
        lines = _merge_orphaned_words(value.split('\n'))
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                i += 1
                continue

            # Section header
            if _is_header(stripped):
                html_parts.append(
                    f'<h4 class="text-sm font-semibold text-slate-200 mt-3 mb-1">'
                    f'{escape(_header_text(stripped))}</h4>'
                )
                i += 1
                continue

            # Bullet item -- collect consecutive bullets into a list
            if _bullet_re.match(line):
                bullet_items = []
                while i < len(lines) and _bullet_re.match(lines[i]):
                    item_text = re.sub(r'^\s*[-*]\s+', '', lines[i].strip())
                    bullet_items.append(
                        f'<li class="mb-0.5">{escape(item_text)}</li>'
                    )
                    i += 1
                html_parts.append(
                    f'<ul class="list-disc list-inside space-y-0.5 ml-1">'
                    f'{"".join(bullet_items)}</ul>'
                )
                continue

            # Regular text line -- render as paragraph
            html_parts.append(f'<p class="mb-1">{escape(stripped)}</p>')
            i += 1

        return Markup('\n'.join(html_parts))

    # --- Blueprint registration ---
    from job_finder.web.blueprints.companies import companies_bp
    from job_finder.web.blueprints.costs import costs_bp
    from job_finder.web.blueprints.dashboard import dashboard_bp
    from job_finder.web.blueprints.detections import detections_bp
    from job_finder.web.blueprints.feedback import feedback_bp
    from job_finder.web.blueprints.jobs import jobs_bp
    from job_finder.web.blueprints.pipeline import pipeline_bp
    from job_finder.web.blueprints.profile import profile_bp
    from job_finder.web.blueprints.resume import resume_bp
    from job_finder.web.blueprints.settings import settings_bp

    # companies_bp, resume_bp, feedback_bp, costs_bp registered BEFORE jobs_bp (catch-all route) to prevent route shadowing
    app.register_blueprint(companies_bp)
    app.register_blueprint(resume_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(costs_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(detections_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(settings_bp)

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
