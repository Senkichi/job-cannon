"""Tests for the admin blueprint (job-runtime control endpoints).

Focus areas:
- 503 path when no scheduler is running (covers the test-mode default).
- run-now max_instances pre-check: rejects with 409 when an instance of the
  target job is already executing, instead of stacking a parallel run.
  This guard prevents the writer-lock starvation pattern surfaced in
  .planning/NEXT_STEPS rev 6→8.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_job(job_id: str, *, next_run_time: datetime | None = None) -> MagicMock:
    """Construct a MagicMock that quacks like APScheduler's Job for our routes."""
    job = MagicMock()
    job.id = job_id
    job.name = job_id
    job.next_run_time = next_run_time or datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    job.trigger = "cron[hour='0,8,16']"
    return job


def _fake_scheduler(
    jobs: dict[str, MagicMock] | None = None,
    *,
    running_counts: dict[str, int] | None = None,
) -> MagicMock:
    """Construct a fake APScheduler with the surface area admin.py reads.

    `running_counts` maps job_id → current concurrent-instance count, mirroring
    ``executor._instances`` (a defaultdict(int) in real APScheduler 3.x).
    """
    jobs = jobs or {}
    sched = MagicMock()
    sched.get_job.side_effect = lambda jid: jobs.get(jid)
    sched.get_jobs.return_value = list(jobs.values())

    # Mirror real APScheduler: ``sched._executors[name]._instances[job_id]`` ints
    executor = MagicMock()
    executor._instances = defaultdict(int)
    if running_counts:
        for jid, count in running_counts.items():
            executor._instances[jid] = count
    sched._executors = {"default": executor}
    return sched


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(app):
    """Reuse the standard test app fixture; admin blueprint is always registered."""
    return app.test_client()


# ---------------------------------------------------------------------------
# 503 path (no scheduler)
# ---------------------------------------------------------------------------


class TestSchedulerNotRunning:
    """When get_scheduler() returns None, every endpoint returns 503.

    Patches get_scheduler explicitly because the module-level _scheduler
    singleton can be set by prior tests in the same session and isn't reset
    between tests.
    """

    def test_list_jobs_returns_503(self, admin_client):
        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=None):
            response = admin_client.get("/admin/jobs")
        assert response.status_code == 503
        assert response.get_json()["error"] == "scheduler not running"

    def test_run_now_returns_503(self, admin_client):
        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=None):
            response = admin_client.post("/admin/jobs/foo/run-now")
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# run-now max_instances pre-check
# ---------------------------------------------------------------------------


class TestRunNowMaxInstancesGuard:
    def test_runs_when_no_instance_in_flight(self, admin_client):
        """Default path: job exists, no instance running → 200 + triggered=True."""
        job = _fake_job("orphan_cleanup")
        sched = _fake_scheduler({"orphan_cleanup": job})

        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=sched):
            response = admin_client.post("/admin/jobs/orphan_cleanup/run-now")

        assert response.status_code == 200
        body = response.get_json()
        assert body == {"id": "orphan_cleanup", "triggered": True}
        # next_run_time should have been modified to a near-now timestamp
        job.modify.assert_called_once()
        modified_kwargs = job.modify.call_args.kwargs
        assert "next_run_time" in modified_kwargs
        assert isinstance(modified_kwargs["next_run_time"], datetime)

    def test_returns_409_when_instance_already_running(self, admin_client):
        """If executor._instances[job_id] > 0, refuse to stack a parallel run."""
        job = _fake_job("reconcile_all_companies")
        sched = _fake_scheduler(
            {"reconcile_all_companies": job},
            running_counts={"reconcile_all_companies": 1},
        )

        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=sched):
            response = admin_client.post("/admin/jobs/reconcile_all_companies/run-now")

        assert response.status_code == 409
        body = response.get_json()
        assert body["id"] == "reconcile_all_companies"
        assert body["triggered"] is False
        assert body["reason"] == "already running"
        # Critically: modify must NOT have been called when we rejected
        job.modify.assert_not_called()

    def test_returns_404_for_unknown_job(self, admin_client):
        """Job not in scheduler → 404 (unchanged behaviour)."""
        sched = _fake_scheduler({})  # empty job registry

        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=sched):
            response = admin_client.post("/admin/jobs/does_not_exist/run-now")

        assert response.status_code == 404
        assert "no such job" in response.get_json()["error"]

    def test_returns_200_when_running_count_was_set_then_cleared(self, admin_client):
        """Defaultdict[int] semantics: a key that was incremented then decremented
        to 0 should NOT block a fresh run."""
        job = _fake_job("orphan_cleanup")
        sched = _fake_scheduler(
            {"orphan_cleanup": job},
            running_counts={"orphan_cleanup": 0},
        )

        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=sched):
            response = admin_client.post("/admin/jobs/orphan_cleanup/run-now")

        assert response.status_code == 200
        job.modify.assert_called_once()

    def test_handles_apscheduler_internals_gracefully(self, admin_client):
        """If APScheduler internals shape changes (no _executors), fall back
        to the legacy fire-and-hope behaviour instead of 500ing."""
        job = _fake_job("orphan_cleanup")
        sched = MagicMock()
        sched.get_job.side_effect = lambda jid: job if jid == "orphan_cleanup" else None
        # Deliberately remove the _executors surface to simulate API drift
        del sched._executors

        with patch("job_finder.web.blueprints.admin.get_scheduler", return_value=sched):
            response = admin_client.post("/admin/jobs/orphan_cleanup/run-now")

        # Falls through to scheduling normally — better than blocking all run-nows
        assert response.status_code == 200
        job.modify.assert_called_once()
