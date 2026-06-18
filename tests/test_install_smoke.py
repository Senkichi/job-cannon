"""Unit test pinning ``EXPECTED_BOOT_JOB_COUNT`` to the live scheduler set (#457).

The CI install-smoke lane (``.github/workflows/install-smoke.yml``) asserts that
``register_all_jobs`` registers exactly ``scripts/install_smoke.py``'s
``EXPECTED_BOOT_JOB_COUNT`` jobs. That constant is a hardcoded value inside the
smoke (which runs in the pipx venv, with no pytest); this test runs in the normal
suite and pins the constant to the *actual* live registration so the two cannot
drift: adding or removing a scheduled job fails this test until the constant is
updated, giving a fast local signal independent of the CI lane.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from apscheduler.schedulers.background import BackgroundScheduler

from job_finder.web.scheduler._jobs import register_all_jobs

# Load the smoke as a module: scripts/ is not an importable package, so resolve
# it by path (same pattern as tests/test_pre_m078_remediation.py).
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_smoke.py"
_spec = importlib.util.spec_from_file_location("install_smoke", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
install_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_smoke)


def _mock_app():
    """Minimal app mock register_all_jobs accepts (mirrors test_scheduler.py).

    Disables the chained predecessors' real work so the introspection never
    touches the network or a real DB; DB_PATH points at a nonexistent file.
    """
    app = MagicMock()
    app.config = {
        "JF_CONFIG": {
            "staleness": {"enabled": False},
            "careers_crawl": {"enabled": False},
        },
        "DB_PATH": "/nonexistent/install_smoke_test.db",
    }
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    app.app_context.return_value = ctx
    return app


def test_expected_boot_job_count_matches_live_registration():
    """EXPECTED_BOOT_JOB_COUNT must equal the live register_all_jobs count.

    Asserts against the live registration (not a hardcoded duplicate), so a job
    added to / removed from register_all_jobs fails this until the constant in
    scripts/install_smoke.py is updated in lockstep.
    """
    sched = BackgroundScheduler()  # unstarted
    register_all_jobs(sched, _mock_app())
    live_count = len(sched.get_jobs())

    assert live_count == install_smoke.EXPECTED_BOOT_JOB_COUNT, (
        f"live register_all_jobs count {live_count} drifted from "
        f"EXPECTED_BOOT_JOB_COUNT={install_smoke.EXPECTED_BOOT_JOB_COUNT}; update the constant in "
        "scripts/install_smoke.py (and the CI install-smoke lane)."
    )


def test_chained_successors_absent_from_boot_registration():
    """The completion-chained successors must not be cron-registered at boot.

    Mirrors the smoke's own scheduler check so the test guards the same invariant
    the CI lane enforces (#229: agentic_backfill/company_linkage are released on a
    predecessor's completion, not scheduled at boot).
    """
    sched = BackgroundScheduler()
    register_all_jobs(sched, _mock_app())
    job_ids = {job.id for job in sched.get_jobs()}

    for chained in install_smoke.CHAINED_SUCCESSORS:
        assert chained not in job_ids, (
            f"{chained!r} must be completion-chained, not cron-registered at boot"
        )
