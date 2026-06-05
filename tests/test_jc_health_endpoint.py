"""Tests for the ``/__jc_health`` identity probe endpoint (Issue #38, Commit B).

Acceptance criteria:
  - Status 200
  - JSON payload data["app"] == "job-cannon"  (load-bearing identity marker)
  - Payload contains ``version``, ``pid``, ``start_time_utc`` keys
  - Endpoint is registered directly on ``app.route``, NOT via a Blueprint
    (assert no ``Blueprint.add_url_rule`` mediates it)
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from job_finder.web import create_app


@pytest.fixture(scope="module")
def health_client():
    """Minimal Flask test client with the /__jc_health endpoint wired up."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = create_app(
            config={
                "TESTING": True,
                "db": {"path": db_path},
            }
        )
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, app
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------------
# Shape + identity marker
# ---------------------------------------------------------------------------


def test_jc_health_status_200(health_client):
    """GET /__jc_health returns HTTP 200."""
    client, _app = health_client
    resp = client.get("/__jc_health")
    assert resp.status_code == 200


def test_jc_health_content_type_json(health_client):
    """Response Content-Type must be JSON."""
    client, _app = health_client
    resp = client.get("/__jc_health")
    assert "application/json" in resp.content_type


def test_jc_health_identity_marker(health_client):
    """data['app'] == 'job-cannon' — the load-bearing identity marker."""
    client, _app = health_client
    resp = client.get("/__jc_health")
    data = resp.get_json()
    assert data is not None
    assert data.get("app") == "job-cannon", (
        "Identity marker 'app' must equal 'job-cannon' exactly — "
        "probe_existing_jc() checks this string."
    )


def test_jc_health_required_keys(health_client):
    """Payload must contain version, pid, start_time_utc."""
    client, _app = health_client
    data = client.get("/__jc_health").get_json()
    assert "version" in data, "Missing 'version' key"
    assert "pid" in data, "Missing 'pid' key"
    assert "start_time_utc" in data, "Missing 'start_time_utc' key"


def test_jc_health_pid_is_our_pid(health_client):
    """pid field must reflect the current process PID."""
    client, _app = health_client
    data = client.get("/__jc_health").get_json()
    assert data["pid"] == os.getpid()


def test_jc_health_start_time_utc_format(health_client):
    """start_time_utc must be a non-empty ISO-format string ending in 'Z'."""
    client, _app = health_client
    data = client.get("/__jc_health").get_json()
    start_time = data.get("start_time_utc", "")
    assert isinstance(start_time, str)
    assert start_time.endswith("Z"), f"Expected UTC ISO string ending in 'Z', got: {start_time!r}"
    assert len(start_time) > 10  # basic sanity: "2026-01-01T..." is at least 20 chars


def test_jc_health_start_time_set_in_app_config(health_client):
    """_JF_START_TIME_UTC must be set in app.config at create_app time."""
    _client, app = health_client
    assert "_JF_START_TIME_UTC" in app.config
    val = app.config["_JF_START_TIME_UTC"]
    assert isinstance(val, str) and val.endswith("Z")


# ---------------------------------------------------------------------------
# Blueprint isolation test
# ---------------------------------------------------------------------------


def test_jc_health_registered_directly_not_via_blueprint(health_client):
    """/__jc_health must NOT be mediated by any Blueprint.

    The route must be registered directly on the Flask app object so that its
    availability is independent of any single blueprint's successful import.
    We verify this by checking that the view function for /__jc_health is NOT
    found in any registered blueprint's url_map rules.
    """
    _client, app = health_client

    # Collect all rules that belong to a blueprint (endpoint contains a '.')
    blueprint_endpoints: set[str] = set()
    for blueprint_name, bp in app.blueprints.items():
        for rule in app.url_map.iter_rules():
            if rule.endpoint.startswith(f"{blueprint_name}."):
                blueprint_endpoints.add(rule.endpoint)

    # Find the /__jc_health endpoint
    health_rule = None
    for rule in app.url_map.iter_rules():
        if rule.rule == "/__jc_health":
            health_rule = rule
            break

    assert health_rule is not None, "/__jc_health must be registered in url_map"
    assert health_rule.endpoint not in blueprint_endpoints, (
        f"/__jc_health endpoint '{health_rule.endpoint}' must NOT belong to a blueprint, "
        "but it was found in a blueprint's rules."
    )


def test_jc_health_reachable_independent_of_blueprints(health_client):
    """End-to-end: the endpoint returns valid JSON even with a minimal config."""
    client, _app = health_client
    resp = client.get("/__jc_health")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["app"] == "job-cannon"
