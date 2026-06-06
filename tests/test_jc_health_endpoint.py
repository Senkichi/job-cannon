"""Tests for the ``/__jc_health`` identity endpoint.

Acceptance criteria:
- Status 200.
- JSON payload has ``data["app"] == "job-cannon"``.
- Payload contains ``version``, ``pid``, ``start_time_utc`` keys.
- Endpoint is registered directly on ``app.route`` — no Blueprint mediates it.
"""

from __future__ import annotations

import inspect
import tempfile

import pytest


@pytest.fixture
def app():
    """Minimal create_app() with a temp DB to avoid touching real user data."""
    import os

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["JOB_CANNON_USER_DATA_DIR"] = tmp
        from job_finder.web import create_app

        application = create_app(config={"TESTING": True, "db": {"path": f"{tmp}/test.db"}})
        application.config["TESTING"] = True
        yield application
        del os.environ["JOB_CANNON_USER_DATA_DIR"]


@pytest.fixture
def client(app):
    return app.test_client()


def test_jc_health_status_200(client):
    """/__jc_health returns HTTP 200."""
    resp = client.get("/__jc_health")
    assert resp.status_code == 200


def test_jc_health_identity_marker(client):
    """data["app"] == "job-cannon" — the load-bearing identity marker."""
    resp = client.get("/__jc_health")
    data = resp.get_json()
    assert isinstance(data, dict)
    assert data["app"] == "job-cannon"


def test_jc_health_required_keys(client):
    """Payload must contain version, pid, start_time_utc."""
    resp = client.get("/__jc_health")
    data = resp.get_json()
    assert "version" in data
    assert "pid" in data
    assert "start_time_utc" in data


def test_jc_health_pid_is_current_process(client):
    """pid field must match the current process PID."""
    import os

    resp = client.get("/__jc_health")
    data = resp.get_json()
    assert data["pid"] == os.getpid()


def test_jc_health_start_time_utc_is_string(client):
    """start_time_utc must be a non-empty string (UTC ISO format)."""
    resp = client.get("/__jc_health")
    data = resp.get_json()
    assert isinstance(data["start_time_utc"], str)
    assert len(data["start_time_utc"]) > 0


def test_jc_health_not_via_blueprint(app):
    """/__jc_health must be registered directly on app.route, not via a Blueprint.

    Verify by inspecting Flask's url_map: the endpoint must be registered
    in app.view_functions under a name that does NOT correspond to any
    registered blueprint prefix.

    Also verifies that the route was added via app.route (not Blueprint.add_url_rule)
    by checking the view function's qualified name — it must be a closure inside
    create_app, not inside a Blueprint class.
    """
    # Verify the route exists in the url_map.
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    assert "/__jc_health" in rules, "/__jc_health must be in app url_map"

    endpoint_name = rules["/__jc_health"].endpoint
    view_fn = app.view_functions[endpoint_name]

    # The view function must NOT have a blueprint prefix in its endpoint name.
    # Blueprint-registered routes are named "<blueprint_name>.<function_name>".
    assert "." not in endpoint_name, (
        f"/__jc_health appears to be registered via a blueprint "
        f"(endpoint name '{endpoint_name}' contains '.')"
    )

    # The function must be defined inside create_app (closure), not in a Blueprint module.
    source_file = inspect.getfile(view_fn)
    assert source_file.endswith("__init__.py"), (
        f"/__jc_health view function should be defined in web/__init__.py, got: {source_file}"
    )
