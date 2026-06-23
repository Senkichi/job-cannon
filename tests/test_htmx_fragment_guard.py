"""Tests for the @htmx_fragment guard — single-point HX-Request enforcement.

Fragment routes render a bare partial (no base.html), so a direct browser hit
must redirect to the parent page instead of surfacing an unstyled orphan. The
guard used to be a hand-copied ``if not request.headers.get("HX-Request")``
idiom present on some fragment routes and silently missing from ~15 others;
``@htmx_fragment`` makes it a single, marker-introspectable enforcement point.
"""

from __future__ import annotations

import pytest
from flask import Flask, url_for

from job_finder.web._htmx import htmx_fragment

# ---------------------------------------------------------------------------
# Decorator unit tests (isolated minimal app — no project wiring)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_app() -> Flask:
    app = Flask(__name__)

    @app.route("/home")
    def home():
        return "HOME"

    @app.route("/frag")
    @htmx_fragment("home")
    def frag():
        return "FRAGMENT"

    return app


def test_decorator_redirects_without_hx_request(mini_app):
    resp = mini_app.test_client().get("/frag")
    assert resp.status_code == 302
    assert "/home" in resp.headers["Location"]


def test_decorator_passes_through_with_hx_request(mini_app):
    resp = mini_app.test_client().get("/frag", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.data == b"FRAGMENT"


def test_decorator_sets_introspection_marker(mini_app):
    view = mini_app.view_functions["frag"]
    assert getattr(view, "_is_htmx_fragment", False) is True
    assert view._htmx_redirect_to == "home"


# ---------------------------------------------------------------------------
# Completeness — every @htmx_fragment route in the real app redirects on a
# non-HTMX GET. Auto-discovers via the marker, so a future fragment route added
# without the decorator simply won't be covered, and one added WITH it is
# enforced for free. The guard fires before any DB access, so the redirect
# branch is testable for every route regardless of entity existence.
# ---------------------------------------------------------------------------


def _arg_value(name: str):
    if name == "dedup_key":
        return "test|key"
    if name.endswith("_id"):
        return 1
    return "1"


def _guarded_rules(app):
    rules = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        if getattr(view, "_is_htmx_fragment", False) and "GET" in (rule.methods or set()):
            rules.append(rule)
    return rules


def test_fragment_routes_are_discovered(client):
    """Sanity floor: the sweep wired a substantial set, not zero."""
    assert len(_guarded_rules(client.application)) >= 20


def test_every_fragment_route_redirects_without_hx_request(client):
    app = client.application
    failures = []
    with app.test_request_context():
        for rule in _guarded_rules(app):
            args = {a: _arg_value(a) for a in rule.arguments}
            url = url_for(rule.endpoint, **args)
            resp = client.get(url)  # deliberately NO HX-Request header
            if resp.status_code != 302:
                failures.append(f"{rule.endpoint} -> {resp.status_code} (expected 302)")
    assert not failures, "Fragment routes not guarding against direct browser hits:\n" + "\n".join(
        failures
    )
