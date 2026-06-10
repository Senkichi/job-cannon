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


# ── apply_url_for Jinja global (Apply-button precedence) ──────────────────────


def _render_apply(app, job) -> str:
    with app.test_request_context():
        return render_template_string("{{ apply_url_for(job) or '' }}", job=job)


def test_apply_global_prefers_strict_direct_url(app):
    out = _render_apply(
        app,
        {
            "direct_url": "https://jobs.lever.co/acme/1",
            "direct_url_confidence": "strict",
            "source_urls": '["https://www.linkedin.com/jobs/view/1"]',
        },
    )
    assert out == "https://jobs.lever.co/acme/1"


def test_apply_global_loose_falls_back_to_source_url(app):
    """Default config (loose_apply_default false): loose stays badge-only."""
    out = _render_apply(
        app,
        {
            "direct_url": "https://jobs.lever.co/acme/1",
            "direct_url_confidence": "loose",
            "source_urls": '["https://www.linkedin.com/jobs/view/1"]',
        },
    )
    assert out == "https://www.linkedin.com/jobs/view/1"


def test_apply_global_aggregator_only_unchanged(app):
    out = _render_apply(app, {"source_urls": '["https://www.linkedin.com/jobs/view/1"]'})
    assert out == "https://www.linkedin.com/jobs/view/1"


def test_apply_global_loose_default_config_flag(tmp_db_path):
    """direct_link.loose_apply_default=true lets a loose link take the Apply slot."""
    from job_finder.web import create_app

    application = create_app(
        config={
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
            "profile": {"target_titles": ["Staff Data Scientist"]},
            "sources": {},
            "direct_link": {"loose_apply_default": True},
        }
    )
    application.config["TESTING"] = True
    with application.test_request_context():
        out = render_template_string(
            "{{ apply_url_for(job) }}",
            job={
                "direct_url": "https://jobs.lever.co/acme/1",
                "direct_url_confidence": "loose",
                "source_urls": '["https://www.linkedin.com/jobs/view/1"]',
            },
        )
    assert out == "https://jobs.lever.co/acme/1"
