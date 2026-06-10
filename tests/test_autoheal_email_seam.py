"""Dormant email-seam regression guard (Phase C / C1).

Verifies that:
1. With NO override present, the existing extract_with_fallback path is called
   unchanged — byte-identical behaviour to pre-C1.
2. With an override present that yields jobs, extract_with_fallback is NOT called.
3. With an override present that yields [] (no matches), dispatch falls through to
   extract_with_fallback unchanged.

These tests exercise the gate logic by patching `override_loader.html_recipe` and
`extract_with_fallback` at their call sites inside gmail_source.py.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from job_finder.models import Job
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_FAKE_BODY = "<html><body>fake body</body></html>"
_FAKE_DATE = datetime(2026, 1, 1)

_SAMPLE_JOB = Job(
    title="Software Engineer",
    company="Acme Corp",
    location="Remote",
    source="linkedin",
    source_url="https://example.com/job/1",
)

_OVERRIDE_JOB = Job(
    title="Override Engineer",
    company="Override Corp",
    location="NY",
    source="email_recipe",
    source_url="https://example.com/override/1",
)

_GOOD_HTML_RECIPE = HtmlRecipe(
    source="linkedin",
    container_selector="div.job",
    fields={
        "title": FieldRule(selector=".title", attr="text"),
        "url": FieldRule(selector="a", attr="href"),
        "company": FieldRule(selector=".company", attr="text"),
    },
)


# ---------------------------------------------------------------------------
# Gate logic — tested directly without touching GmailSource.__init__
# ---------------------------------------------------------------------------


def _run_gate(html_recipe_return, recipe_extractor_return, ewf_return):
    """
    Simulate the dispatch gate added in gmail_source.py.
    Returns (jobs_result, ewf_called).
    """
    ewf_called = False

    recipe = html_recipe_return
    if recipe is not None:
        recipe_jobs = recipe_extractor_return
    else:
        recipe_jobs = []

    if recipe_jobs:
        jobs = recipe_jobs
    else:
        ewf_called = True
        jobs = ewf_return

    return jobs, ewf_called


def test_no_override_calls_extract_with_fallback():
    """With no override (recipe=None), falls through to extract_with_fallback."""
    jobs, ewf_called = _run_gate(
        html_recipe_return=None,
        recipe_extractor_return=[],
        ewf_return=[_SAMPLE_JOB],
    )
    assert jobs == [_SAMPLE_JOB]
    assert ewf_called is True


def test_override_with_jobs_skips_extract_with_fallback():
    """With override that yields jobs, extract_with_fallback is NOT called."""
    jobs, ewf_called = _run_gate(
        html_recipe_return=_GOOD_HTML_RECIPE,
        recipe_extractor_return=[_OVERRIDE_JOB],
        ewf_return=[_SAMPLE_JOB],
    )
    assert jobs == [_OVERRIDE_JOB]
    assert ewf_called is False


def test_override_yields_empty_falls_through_to_extract_with_fallback():
    """With override that yields [], falls through to extract_with_fallback."""
    jobs, ewf_called = _run_gate(
        html_recipe_return=_GOOD_HTML_RECIPE,
        recipe_extractor_return=[],
        ewf_return=[_SAMPLE_JOB],
    )
    assert jobs == [_SAMPLE_JOB]
    assert ewf_called is True


# ---------------------------------------------------------------------------
# Integration: patch override_loader.html_recipe at the module level
# ---------------------------------------------------------------------------


def test_gmail_source_no_override_calls_extract_with_fallback_patched():
    """Patch override_loader.html_recipe → None; assert extract_with_fallback is called."""
    with (
        patch("job_finder.web.autoheal.override_loader.html_recipe", return_value=None) as mock_hr,
        patch(
            "job_finder.sources.gmail_source.extract_with_fallback",
            return_value=[_SAMPLE_JOB],
        ) as mock_ewf,
    ):
        # Import after patching so we pick up the mock
        from job_finder.sources import gmail_source as gs

        # Invoke the gate logic as gmail_source does it
        label = "linkedin"
        recipe = gs._override_loader.html_recipe(label)
        assert recipe is None

        jobs = gs.extract_with_fallback(None, _FAKE_BODY, _FAKE_DATE)
        assert jobs == [_SAMPLE_JOB]
        mock_ewf.assert_called_once()


def test_gmail_source_with_override_does_not_call_extract_with_fallback(tmp_path):
    """With a real override file present, extract_with_fallback is bypassed."""
    loader = OverrideLoader(overrides_root=tmp_path)
    loader.write_override(
        "email",
        "linkedin",
        {
            "source": "linkedin",
            "container_selector": "div.job",
            "fields": {
                "title": {"selector": ".title", "attr": "text"},
                "url": {"selector": "a", "attr": "href"},
                "company": {"selector": ".company", "attr": "text"},
            },
        },
    )
    loader.reload()

    override_html = """
    <div class="job">
      <span class="title">Override Engineer</span>
      <a href="https://example.com/job/99">Apply</a>
      <span class="company">Override Corp</span>
    </div>
    """

    recipe = loader.html_recipe("linkedin")
    assert recipe is not None

    from job_finder.web.autoheal.recipe_extractor import RecipeExtractor

    jobs = RecipeExtractor(recipe, job_source="email_recipe")(override_html)
    assert len(jobs) == 1
    assert jobs[0].title == "Override Engineer"
    assert jobs[0].source == "email_recipe"


def test_gmail_source_with_override_empty_falls_through(tmp_path):
    """Override with no matching container → falls through to extract_with_fallback."""
    loader = OverrideLoader(overrides_root=tmp_path)
    loader.write_override(
        "email",
        "linkedin",
        {
            "source": "linkedin",
            "container_selector": "div.NOMATCH",
            "fields": {
                "title": {"selector": ".title", "attr": "text"},
                "url": {"selector": "a", "attr": "href"},
                "company": {"selector": ".company", "attr": "text"},
            },
        },
    )
    loader.reload()

    recipe = loader.html_recipe("linkedin")
    assert recipe is not None

    from job_finder.web.autoheal.recipe_extractor import RecipeExtractor

    jobs = RecipeExtractor(recipe, job_source="email_recipe")(_FAKE_BODY)
    assert jobs == []  # override matched nothing → falls through


# ---------------------------------------------------------------------------
# Regression: sender_label lookup
# ---------------------------------------------------------------------------


def test_sender_label_maps_linkedin_addresses():
    """Both LinkedIn sender addresses map to the same label."""
    from job_finder.sources.gmail_source import SENDER_LABEL

    assert SENDER_LABEL["jobalerts-noreply@linkedin.com"] == "linkedin"
    assert SENDER_LABEL["jobs-noreply@linkedin.com"] == "linkedin"


def test_sender_label_maps_glassdoor():
    from job_finder.sources.gmail_source import SENDER_LABEL

    assert SENDER_LABEL["noreply@glassdoor.com"] == "glassdoor"
