"""Regression test for dashboard `_get_quick_actions_context`.

Guards the call-site contract between the dashboard view and the
`model_provider` workload validator. Phase 40 (commit 5ec70ef) tightened
`resolve_workload_routing` to reject any tier name not in
`_VALID_WORKLOADS = {"quick", "score", "triage"}`. The dashboard was left
passing the legacy literal `"scoring"`, so every page load of `/` raised
`ValueError: Unknown workload: 'scoring'`.

This test calls `_get_quick_actions_context` end-to-end and asserts no
`ValueError` escapes. If the call site drifts back to a non-workload
literal, this test fails before reaching production.
"""

from job_finder.web.blueprints.dashboard import _get_quick_actions_context
from job_finder.web.db_helpers import get_db
from job_finder.web.model_provider import _VALID_WORKLOADS


def test_get_quick_actions_context_uses_valid_workload(app):
    """The dashboard must request availability for a workload in _VALID_WORKLOADS.

    Reproduces the original crash: visiting `/` invokes
    `_get_quick_actions_context`, which calls `_cached_tier_available`,
    which routes through `resolve_workload_routing`. Any tier name outside
    `_VALID_WORKLOADS` raises ValueError. The fix renamed the literal from
    "scoring" -> "score"; this test guards against re-introduction.
    """
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        cfg = app.config.get("JF_CONFIG", {})

        ctx = _get_quick_actions_context(conn, cfg)

    assert set(ctx.keys()) >= {
        "active_sync",
        "active_scoring",
        "unscored_count",
        "scoring_available",
    }
    assert isinstance(ctx["scoring_available"], bool)


def test_score_is_a_valid_workload():
    """Pins the workload name the dashboard relies on.

    If a future refactor renames the "score" workload (e.g. back to
    "scoring" or forward to something else), the dashboard call site
    must move in lockstep — and this assertion forces the conversation.
    """
    assert "score" in _VALID_WORKLOADS
