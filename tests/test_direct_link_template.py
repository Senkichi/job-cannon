"""Template-render tests for the direct-link badge."""

from __future__ import annotations

from flask import render_template_string


def _render(app, direct_url, confidence):
    with app.test_request_context():
        return render_template_string(
            '{% include "jobs/_direct_link_badge.html" %}',
            job={"direct_url": direct_url, "direct_url_confidence": confidence},
        )


def test_badge_renders_strict(app):
    html = _render(app, "https://jobs.lever.co/acme/1", "strict")
    assert "https://jobs.lever.co/acme/1" in html
    assert "Company posting" in html
    assert "likely" not in html.lower()


def test_badge_renders_loose_with_likely_tag(app):
    html = _render(app, "https://jobs.lever.co/acme/1", "loose")
    assert "https://jobs.lever.co/acme/1" in html
    assert "likely" in html.lower()


def test_badge_absent_when_no_direct_url(app):
    html = _render(app, None, None)
    assert "Company posting" not in html
