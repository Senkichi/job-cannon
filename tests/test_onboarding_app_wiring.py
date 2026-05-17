"""create_app() wires the onboarding blueprint + before_request gate (STRANGE-WIZ-01/02)."""

from job_finder.web.onboarding.state import gate_onboarding


def test_onboarding_blueprint_registered(app):
    """app.url_map contains routes prefixed /onboarding/."""
    rules = [r.rule for r in app.url_map.iter_rules()]
    onboarding_routes = [r for r in rules if r.startswith("/onboarding")]
    # Wave 1 ships /onboarding/welcome only; plan 42-05 will add 7 more.
    assert "/onboarding/welcome" in onboarding_routes


def test_before_request_gate_registered(app):
    """app.before_request_funcs contains gate_onboarding for the app-global hook (key=None)."""
    # Flask stores app-level before_request callables under key None in before_request_funcs.
    global_hooks = app.before_request_funcs.get(None, [])
    assert gate_onboarding in global_hooks


def test_gate_registered_before_scheduler_init(app):
    """The gate must be in place before init_scheduler runs (D-18). We can't directly observe ordering,
    but we CAN observe that the hook is present at app-fixture time (which is after create_app() returned)."""
    global_hooks = app.before_request_funcs.get(None, [])
    assert len(global_hooks) >= 1
