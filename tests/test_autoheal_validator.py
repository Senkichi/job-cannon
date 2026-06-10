"""Tests for the autoheal VALIDATE gate — subprocess corpus replay (Phase C / C4).

The subprocess exists for the wall-clock timeout (ReDoS guard), not isolation.
A hanging regex must be killed by the timeout and must NOT hang this test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from job_finder.web.autoheal import validator
from job_finder.web.autoheal.recipe_schema import AtsAliasRecipe, FieldRule, HtmlRecipe

# ---------------------------------------------------------------------------
# Fixtures — inline HTML / JSON samples, no network, no DB
# ---------------------------------------------------------------------------

_WORKING_SAMPLE = """
<div class="job">
  <span class="title">Platform Engineer</span>
  <a href="https://example.com/job/1">Apply</a>
  <span class="company">Acme</span>
</div>
"""

_FAILING_SAMPLE = """
<div class="job">
  <span class="headline">Platform Engineer</span>
  <a href="https://example.com/job/2">Apply</a>
  <span class="company">Acme</span>
</div>
"""


def _email_recipe(title_selector: str, title_regex: str | None = None) -> HtmlRecipe:
    return HtmlRecipe(
        source="syntheticsource",
        container_selector="div.job",
        fields={
            "title": FieldRule(selector=title_selector, attr="text", regex=title_regex),
            "url": FieldRule(selector="a", attr="href"),
            "company": FieldRule(selector=".company", attr="text"),
        },
    )


# ---------------------------------------------------------------------------
# Email surface
# ---------------------------------------------------------------------------


def test_good_candidate_passes():
    """Handles both old and new markup → Passed."""
    verdict = validator.validate(
        _email_recipe(".title, .headline"),
        "email",
        corpus_samples=[_WORKING_SAMPLE],
        failing_samples=[_FAILING_SAMPLE],
        timeout_s=30,
    )
    assert verdict.ok
    assert verdict.reason is None


def test_regressing_candidate_rejected():
    """Only handles the new markup → breaks the prior-working sample."""
    verdict = validator.validate(
        _email_recipe(".headline"),
        "email",
        corpus_samples=[_WORKING_SAMPLE],
        failing_samples=[_FAILING_SAMPLE],
        timeout_s=30,
    )
    assert not verdict.ok
    assert verdict.reason == "regression"


def test_under_fixing_candidate_rejected():
    """Still yields nothing on the failing sample → target_unfixed."""
    verdict = validator.validate(
        _email_recipe(".title"),
        "email",
        corpus_samples=[_WORKING_SAMPLE],
        failing_samples=[_FAILING_SAMPLE],
        timeout_s=30,
    )
    assert not verdict.ok
    assert verdict.reason == "target_unfixed"


def test_hanging_regex_rejected_by_timeout():
    """Catastrophic-backtracking regex is killed by the subprocess timeout."""
    redos_title = "a" * 40 + "!"
    sample = f"""
    <div class="job">
      <span class="title">{redos_title}</span>
      <a href="https://example.com/job/3">Apply</a>
      <span class="company">Acme</span>
    </div>
    """
    verdict = validator.validate(
        _email_recipe(".title", title_regex=r"(a+)+$"),
        "email",
        corpus_samples=[sample],
        failing_samples=[sample],
        timeout_s=3,
    )
    assert not verdict.ok
    assert verdict.reason == "timeout"


# ---------------------------------------------------------------------------
# ATS surface
# ---------------------------------------------------------------------------

_ATS_WORKING = '[{"text": "Engineer", "hostedUrl": "https://jobs.lever.co/a/1"}]'
_ATS_FAILING = '[{"text": "Engineer", "renamedUrl": "https://jobs.lever.co/a/2"}]'


def test_ats_alias_candidate_passes():
    candidate = AtsAliasRecipe(
        source="ats:syntheticlever", title_fields=[], url_fields=["renamedUrl"], array_keys=[]
    )
    verdict = validator.validate(
        candidate,
        "ats",
        corpus_samples=[_ATS_WORKING],
        failing_samples=[_ATS_FAILING],
        timeout_s=30,
    )
    assert verdict.ok


def test_ats_wrong_alias_rejected_target_unfixed():
    candidate = AtsAliasRecipe(
        source="ats:syntheticlever", title_fields=[], url_fields=["wrongKey"], array_keys=[]
    )
    verdict = validator.validate(
        candidate,
        "ats",
        corpus_samples=[_ATS_WORKING],
        failing_samples=[_ATS_FAILING],
        timeout_s=30,
    )
    assert not verdict.ok
    assert verdict.reason == "target_unfixed"


# ---------------------------------------------------------------------------
# Optional pytest gate (c) — skip cleanly when no matching test exists
# ---------------------------------------------------------------------------


def test_pytest_gate_skips_when_no_matching_test():
    assert validator._pytest_gate("nosuchtokenxyz", timeout_s=5) is None


def test_pytest_gate_runs_matching_tests_and_maps_outcome():
    with patch.object(
        validator.subprocess, "run", return_value=SimpleNamespace(returncode=0)
    ) as mock_run:
        assert validator._pytest_gate("linkedin", timeout_s=5) is None
    # A matching test file exists for linkedin, so pytest must have been invoked
    assert mock_run.called

    with patch.object(validator.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
        assert validator._pytest_gate("linkedin", timeout_s=5) == "pytest_failed"


def test_pytest_gate_strips_ats_prefix():
    """ats:linkedin → token 'linkedin' (':' is not a valid -k / filename char)."""
    with patch.object(
        validator.subprocess, "run", return_value=SimpleNamespace(returncode=0)
    ) as mock_run:
        validator._pytest_gate("ats:linkedin", timeout_s=5)
    assert mock_run.called
