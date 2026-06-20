#!/usr/bin/env python
"""Install-only RUNTIME smoke for job-cannon (issue #457).

Runs INSIDE the pipx venv interpreter, so it carries no pytest dependency.
Boots the installed app against a clean temp user-data dir with ZERO
credentials and asserts the fresh-install runtime invariants that the existing
``pipx --help`` smoke (install-validate.yml) never reaches:

  1. Health endpoint -- ``GET /__jc_health`` returns HTTP 200 with
     ``{"app": "job-cannon", "pid": <this process>}``. Uses Flask's test client
     (no real port bind -- avoids OS-specific port/firewall flakiness on runners).
  2. Scheduler registration -- ``register_all_jobs`` registers exactly
     ``EXPECTED_BOOT_JOB_COUNT`` cron/interval jobs, and the two
     completion-chained successors (``agentic_backfill``/``company_linkage``)
     are NOT among them (they are released on a predecessor's completion, #229).
  3. ``$0`` mocked ingestion -- ``run_ingestion`` with every source disabled AND
     ``GmailSource``/``SerpAPISource`` mocked returns a dict with ``jobs_new == 0``
     and raises nothing. No provider/network/credential call is made.

Exit 0 on success; non-zero with a one-line reason on the first failed check.

NOTE on EXPECTED_BOOT_JOB_COUNT: this is the *live* count ``register_all_jobs``
produces today (15), NOT the 13 enumerated in issue #457's body. That list
predates the serve-path liveness ``heartbeat`` job (#435, registered by
``register_heartbeat``), which is distinct from the daily ``health_heartbeat``
DB writer. ``tests/test_install_smoke.py`` pins this constant to the live
registration so it cannot silently drift when a scheduled job is added/removed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Live count of cron/interval jobs register_all_jobs() adds at boot. Pinned to
# the live registration by tests/test_install_smoke.py -- adding or removing a
# scheduler job forces a matching update here (and to the CI smoke lane).
# 15 since the daily jd-content LLM adjudication backfill (register_jd_adjudication).
EXPECTED_BOOT_JOB_COUNT = 15

# Completion-chained successors that must NOT be cron-registered at boot (#229).
CHAINED_SUCCESSORS: tuple[str, ...] = ("agentic_backfill", "company_linkage")


class SmokeFailure(AssertionError):
    """A smoke invariant failed; the message is a one-line reason for stderr."""


def _build_config(db_path: str) -> dict:
    """Config with every source disabled so no fetch touches the network.

    ``SKIP_SCHEDULER`` keeps create_app from spinning up a real background
    scheduler (and trips the startup-backfill / file-logging / keyring guards),
    while ``TESTING`` stays False to exercise the genuine production boot path.
    """
    return {
        "db": {"path": db_path},
        "TESTING": False,
        "SKIP_SCHEDULER": True,
        "sources": {
            "imap": {"enabled": False},
            "gmail": {"enabled": False},
            "serpapi": {"enabled": False},
            "thordata": {"enabled": False},
            "dataforseo": {"enabled": False},
            "portal_search": {"enabled": False},
        },
        "profile": {
            "target_titles": [],
            "target_locations": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "industries": [],
            "skills": [],
        },
        "scoring": {"min_score_threshold": 0},
    }


def check_health(app) -> None:
    """Assert GET /__jc_health is reachable, 200, and identifies this process."""
    client = app.test_client()
    resp = client.get("/__jc_health")
    if resp.status_code != 200:
        raise SmokeFailure(f"health: status {resp.status_code} != 200")
    data = resp.get_json(silent=True)
    if data is None:
        raise SmokeFailure("health: response body was not JSON")
    if data.get("app") != "job-cannon":
        raise SmokeFailure(f"health: app field {data.get('app')!r} != 'job-cannon'")
    if data.get("pid") != os.getpid():
        raise SmokeFailure(f"health: pid {data.get('pid')!r} != os.getpid() {os.getpid()}")


def check_scheduled_jobs(app) -> None:
    """Assert register_all_jobs registers the expected boot job set."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from job_finder.web.scheduler._jobs import register_all_jobs

    sched = BackgroundScheduler()  # unstarted -- introspect, never run
    register_all_jobs(sched, app)
    job_ids = [job.id for job in sched.get_jobs()]

    if len(job_ids) != EXPECTED_BOOT_JOB_COUNT:
        raise SmokeFailure(
            f"scheduler: registered {len(job_ids)} jobs {sorted(job_ids)} "
            f"!= EXPECTED_BOOT_JOB_COUNT={EXPECTED_BOOT_JOB_COUNT}"
        )
    for chained in CHAINED_SUCCESSORS:
        if chained in job_ids:
            raise SmokeFailure(
                f"scheduler: {chained!r} must be completion-chained, not cron-registered at boot"
            )


def check_mocked_ingestion(db_path: str, config: dict) -> None:
    """Run one $0 fully-mocked ingestion and assert it is clean and zero-yield."""
    with (
        mock.patch("job_finder.web.ingestion_runner.GmailSource") as mock_gmail,
        mock.patch("job_finder.sources.serpapi_source.SerpAPISource") as mock_serpapi,
    ):
        mock_gmail.return_value.fetch_jobs.return_value = ([], set())
        mock_serpapi.return_value.fetch_jobs.return_value = []

        from job_finder.web.pipeline_runner import run_ingestion

        summary = run_ingestion(db_path, config)

        # Sources disabled => the source classes must never even be constructed.
        if mock_gmail.called:
            raise SmokeFailure(
                "ingestion: GmailSource was instantiated despite sources.gmail.enabled=False"
            )
        if mock_serpapi.called:
            raise SmokeFailure(
                "ingestion: SerpAPISource was instantiated despite sources.serpapi.enabled=False"
            )

    if not isinstance(summary, dict):
        raise SmokeFailure(
            f"ingestion: run_ingestion returned {type(summary).__name__}, not a dict"
        )
    if "jobs_new" not in summary:
        raise SmokeFailure("ingestion: summary is missing the 'jobs_new' key")
    if summary["jobs_new"] != 0:
        raise SmokeFailure(
            f"ingestion: jobs_new {summary['jobs_new']} != 0 (no source should yield jobs)"
        )


def run_smoke() -> None:
    """Run every smoke check against a clean temp user-data dir.

    Raises SmokeFailure (or any unexpected exception) on the first failure.
    """
    prior_user_data_dir = os.environ.get("JOB_CANNON_USER_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="jc-install-smoke-") as tmp:
        os.environ["JOB_CANNON_USER_DATA_DIR"] = tmp
        try:
            db_path = str(Path(tmp) / "jobs.db")
            config = _build_config(db_path)

            from job_finder.web import create_app

            app = create_app(config=config)

            check_health(app)
            check_scheduled_jobs(app)
            # create_app resolves the canonical DB_PATH (and ran migrations on it).
            check_mocked_ingestion(app.config["DB_PATH"], config)
        finally:
            if prior_user_data_dir is None:
                os.environ.pop("JOB_CANNON_USER_DATA_DIR", None)
            else:
                os.environ["JOB_CANNON_USER_DATA_DIR"] = prior_user_data_dir


def main() -> int:
    try:
        run_smoke()
    except SmokeFailure as exc:
        print(f"INSTALL SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # an unexpected boot/import error is also a failure
        print(f"INSTALL SMOKE FAILED (unexpected): {exc!r}", file=sys.stderr)
        return 1
    print(
        f"INSTALL SMOKE OK: /__jc_health 200, {EXPECTED_BOOT_JOB_COUNT} scheduled jobs, "
        "$0 mocked ingestion clean (jobs_new=0)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
