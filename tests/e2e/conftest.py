"""E2E test fixtures — Playwright browser tests against a live Flask server.

The 'from playwright.sync_api' import triggers nit-pick-supreme's browser_e2e.py
engine detection (it rglobs for conftest.py files containing 'playwright').
"""

import socket
import sqlite3
import tempfile
import threading
import time
import os
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import Page  # noqa: F401 — triggers browser_e2e detection

from job_finder.web import create_app
from job_finder.web.db_migrate import run_migrations

E2E_PORT = 5001


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    """Block until localhost:port accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = socket.create_connection(("localhost", port), timeout=1)
            conn.close()
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.25)
    raise RuntimeError(f"Flask server did not start on port {port} within {timeout}s")


def _populate_sample_data(db_path: str) -> None:
    """Insert sample jobs so E2E pages have visible content."""
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()

    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, user_interest,
             pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "stripe|senior data scientist|remote",
                "Senior Data Scientist",
                "Stripe",
                "Remote",
                '["linkedin"]',
                '["https://linkedin.com/jobs/view/1111/"]',
                "1111",
                180000, 240000,
                "Build data products at Stripe. Looking for ML expertise.",
                week_ago, now, 8.5, '{"skills": 0.9}', "interested", "reviewing",
            ),
            (
                "acme|data engineer|new york ny",
                "Data Engineer",
                "Acme Corp",
                "New York, NY",
                '["glassdoor"]',
                '["https://glassdoor.com/job/2222"]',
                "2222",
                150000, 200000,
                "Design and maintain data pipelines.",
                week_ago, now, 7.0, '{"skills": 0.7}', "unreviewed", None,
            ),
            (
                "widgetco|staff ml engineer|san francisco ca",
                "Staff ML Engineer",
                "WidgetCo",
                "San Francisco, CA",
                '["linkedin"]',
                '["https://linkedin.com/jobs/view/3333/"]',
                "3333",
                200000, 280000,
                "Lead machine learning team at WidgetCo.",
                week_ago, now, 9.1, '{"skills": 0.95}', "interested", "applied",
            ),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def e2e_db_path():
    """Create a temp DB with migrations and sample data for the E2E session."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    _populate_sample_data(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture(scope="session")
def live_server(e2e_db_path):
    """Start Flask in a background thread on port 5001 for the test session."""
    test_config = {
        "db": {"path": e2e_db_path},
        "scoring": {
            "min_score_threshold": 40,
            "monthly_budget_usd": 25.0,
        },
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    app = create_app(config=test_config)
    app.config["TESTING"] = True

    server_thread = threading.Thread(
        target=app.run,
        kwargs={"port": E2E_PORT, "use_reloader": False},
        daemon=True,
    )
    server_thread.start()
    _wait_for_port(E2E_PORT)

    yield f"http://localhost:{E2E_PORT}"


@pytest.fixture(scope="session")
def base_url(live_server):
    """Override pytest-playwright's base_url fixture."""
    return live_server
