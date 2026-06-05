"""Tests for the ``/__jc_health`` Flask endpoint.

Acceptance criteria:
- status 200
- JSON payload ``data["app"] == "job-cannon"``
- payload contains ``version``, ``pid``, ``start_time_utc`` keys
- endpoint registered directly on ``app.route`` (not via a blueprint —
  assert no ``Blueprint.add_url_rule`` mediates it)
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def jc_app(tmp_path):
    """Minimal create_app() instance wired for the health tests."""
    from job_finder.web import create_app

    app = create_app(
        config={
            "TESTING": True,
            "db": {"path": str(tmp_path / "test.db")},
        }
    )
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(jc_app):
    with jc_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------


class TestJcHealthEndpoint:
    def test_status_200(self, client) -> None:
        response = client.get("/__jc_health")
        assert response.status_code == 200

    def test_content_type_json(self, client) -> None:
        response = client.get("/__jc_health")
        assert "application/json" in response.content_type

    def test_app_field_is_job_cannon(self, client) -> None:
        """The identity marker 'app' == 'job-cannon' must be present."""
        response = client.get("/__jc_health")
        data = response.get_json()
        assert data is not None
        assert data["app"] == "job-cannon"

    def test_version_key_present(self, client) -> None:
        response = client.get("/__jc_health")
        data = response.get_json()
        assert "version" in data

    def test_pid_key_present(self, client) -> None:
        response = client.get("/__jc_health")
        data = response.get_json()
        assert "pid" in data

    def test_start_time_utc_key_present(self, client) -> None:
        response = client.get("/__jc_health")
        data = response.get_json()
        assert "start_time_utc" in data

    def test_pid_matches_current_process(self, client) -> None:
        """pid field must equal os.getpid() (single-process test run)."""
        response = client.get("/__jc_health")
        data = response.get_json()
        assert data["pid"] == os.getpid()

    def test_start_time_utc_is_iso_string(self, client) -> None:
        """start_time_utc must be a non-empty ISO-8601 UTC string."""
        response = client.get("/__jc_health")
        data = response.get_json()
        start = data.get("start_time_utc", "")
        assert isinstance(start, str) and len(start) > 0
        # Should look like "2026-01-01T00:00:00Z" or similar
        assert "T" in start or start == ""


# ---------------------------------------------------------------------------
# Blueprint isolation test
# ---------------------------------------------------------------------------


class TestHealthEndpointNotInBlueprint:
    """Assert the endpoint is registered directly on the app, not via a blueprint."""

    def test_route_registered_on_app_not_blueprint(self, jc_app) -> None:
        """/__jc_health must appear in app.url_map and NOT be routed via a Blueprint.

        We verify this by checking:
        1. The route exists in the app's URL map.
        2. The endpoint name is NOT prefixed with any blueprint name
           (Flask sets endpoint to ``<blueprint_name>.<function_name>`` for
           blueprint-registered routes; direct ``@app.route`` uses the bare
           function name).
        """
        # Collect all rules for /__jc_health
        health_rules = [
            rule for rule in jc_app.url_map.iter_rules() if rule.rule == "/__jc_health"
        ]
        assert health_rules, "/__jc_health must be registered in the URL map"

        for rule in health_rules:
            endpoint = rule.endpoint
            # A blueprint-registered endpoint would look like "bp_name.func_name".
            # A direct app.route endpoint is just the function name (no dot).
            assert "." not in endpoint, (
                f"/__jc_health endpoint '{endpoint}' looks like a blueprint route "
                "(contains '.').  It must be registered directly on app, not via "
                "a Blueprint."
            )

    def test_no_blueprint_owns_health_endpoint(self, jc_app) -> None:
        """The view function for /__jc_health must not be found in any blueprint."""

        health_rules = [
            rule for rule in jc_app.url_map.iter_rules() if rule.rule == "/__jc_health"
        ]
        assert health_rules

        endpoint_name = health_rules[0].endpoint
        # The view function must be reachable via app.view_functions
        assert endpoint_name in jc_app.view_functions

        # Verify that no registered Blueprint claims this endpoint name.
        # Blueprint endpoints are stored as "<blueprint_name>.<endpoint>".
        for bp_name, _bp in jc_app.blueprints.items():
            assert not endpoint_name.startswith(f"{bp_name}."), (
                f"Blueprint '{bp_name}' appears to own the /__jc_health endpoint."
            )
