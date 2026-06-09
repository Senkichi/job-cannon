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
def app(monkeypatch):
    """Minimal create_app() with a temp DB to avoid touching real user data.

    Uses ``monkeypatch.setenv`` (NOT raw ``os.environ[...] = ...`` + ``del``):
    monkeypatch restores ``JOB_CANNON_USER_DATA_DIR`` to its *prior* value at
    teardown. The old raw-``del`` version unconditionally unset the var, so when
    a dev session / CI had it pointing at a real config, every test that ran
    after this one resolved a partial platformdirs config and failed in
    ``load_config()`` — order-dependent cross-test pollution (Issue #40 fix).
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", tmp)
        from job_finder.web import create_app

        application = create_app(config={"TESTING": True, "db": {"path": f"{tmp}/test.db"}})
        application.config["TESTING"] = True
        yield application


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
    """start_time_utc must be a non-empty naive UTC ISO string.

    Issue #233 (N9): the stamp must come from utc_now_iso() — a *naive* ISO
    datetime with no "Z" suffix and no ``+HH:MM`` offset. That locks the
    store-UTC-render-local invariant at the producer site so any future
    consumer that parses the value cannot be misled into thinking it is
    timezone-aware.
    """
    from datetime import datetime

    resp = client.get("/__jc_health")
    data = resp.get_json()
    value = data["start_time_utc"]
    assert isinstance(value, str)
    assert len(value) > 0
    # Must not advertise a timezone — the value is naive UTC.
    assert not value.endswith("Z"), (
        f"start_time_utc must be a naive ISO datetime (no 'Z' suffix), got: {value!r}"
    )
    assert "+" not in value, f"start_time_utc must not carry a UTC offset, got: {value!r}"
    # Parses as a naive datetime via fromisoformat (no tzinfo attached).
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is None, (
        f"start_time_utc must parse to a naive datetime, got tzinfo={parsed.tzinfo!r}"
    )


def test_create_app_does_not_emit_deprecation_warning(monkeypatch):
    """Issue #233 (N9): create_app must not trip a DeprecationWarning.

    Specifically the start-time stamp must use the canonical ``utc_now_iso()``
    producer, not the deprecated ``datetime.utcnow()`` path.
    """
    import tempfile
    import warnings

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", tmp)
        from job_finder.web import create_app

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_app(config={"TESTING": True, "db": {"path": f"{tmp}/test.db"}})

        utcnow_warnings = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning) and "utcnow" in str(w.message).lower()
        ]
        assert utcnow_warnings == [], (
            "create_app() must not call the deprecated datetime.utcnow() — "
            f"observed: {[str(w.message) for w in utcnow_warnings]}"
        )


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
