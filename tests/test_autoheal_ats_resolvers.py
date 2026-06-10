"""Tests for C2: ATS override-aware resolvers in _field_alias.py.

Covers:
- Task 6: resolve_title, resolve_url, resolve_job_array — no-override regression guard
  and override-extras-appended-after-canonical behaviour.
- Task 7: _platforms_greenhouse + _platforms_lever produce identical canonical output
  to pre-C2 when no override is present.

CARDINAL CONSTRAINT: with NO override file, greenhouse/lever resolve byte-for-byte
as today (Lever text/hostedUrl, Greenhouse title/absolute_url).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from job_finder.web._field_alias import (
    JOB_TITLE_FIELDS,
    JOB_URL_FIELDS,
    extract_field,
    find_job_array,
    resolve_job_array,
    resolve_title,
    resolve_url,
)

# ---------------------------------------------------------------------------
# Helpers — sample postings
# ---------------------------------------------------------------------------

# Greenhouse posting with canonical keys
GREENHOUSE_POSTING = {
    "title": "Senior Software Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
    "location": {"name": "San Francisco, CA"},
    "id": 123,
}

# Lever posting with canonical keys
LEVER_POSTING = {
    "text": "Backend Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abc-def",
    "id": "abc-def",
    "categories": {"location": "New York"},
}

# Posting with a custom renamed key (simulates a field-renamed platform)
RENAMED_URL_POSTING = {
    "text": "Data Scientist",
    "jobUrl": "https://jobs.lever.co/acme/xyz-123",
    "id": "xyz-123",
}

# Posting with ONLY canonical key (should still resolve even with an override present)
CANONICAL_URL_POSTING = {
    "text": "ML Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/canon-456",
    "id": "canon-456",
}

# Data with job array under canonical key
JOBS_DATA = {"jobs": [GREENHOUSE_POSTING]}
LEVER_DATA = [LEVER_POSTING]  # Lever returns a raw list

# Data with a renamed array key
RENAMED_ARRAY_DATA = {"openPositions": [GREENHOUSE_POSTING]}


# ---------------------------------------------------------------------------
# Fixtures — AtsAliasRecipe mocks
# ---------------------------------------------------------------------------


def _make_alias_recipe(
    source: str = "ats:lever",
    title_fields: list[str] | None = None,
    url_fields: list[str] | None = None,
    array_keys: list[str] | None = None,
):
    """Return a minimal AtsAliasRecipe-like object for testing."""
    from job_finder.web.autoheal.recipe_schema import AtsAliasRecipe

    return AtsAliasRecipe(
        source=source,
        title_fields=title_fields or [],
        url_fields=url_fields or [],
        array_keys=array_keys or [],
    )


# ---------------------------------------------------------------------------
# Task 6 — resolve_title: no-override regression guard
# ---------------------------------------------------------------------------


class TestResolveTitleNoOverride:
    """With no override, resolve_title == extract_field(posting, JOB_TITLE_FIELDS)."""

    def test_lever_canonical_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_title(LEVER_POSTING, "lever")
        expected = extract_field(LEVER_POSTING, JOB_TITLE_FIELDS)
        assert result == expected
        assert result == "Backend Engineer"

    def test_greenhouse_canonical_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_title(GREENHOUSE_POSTING, "greenhouse")
        expected = extract_field(GREENHOUSE_POSTING, JOB_TITLE_FIELDS)
        assert result == expected
        assert result == "Senior Software Engineer"

    def test_missing_title_returns_none_no_override(self):
        posting = {"hostedUrl": "https://jobs.lever.co/x"}
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_title(posting, "lever")
        assert result is None

    def test_unknown_platform_no_override(self):
        """Unknown platform with no override still falls through to canonical."""
        posting = {"title": "SRE"}
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_title(posting, "unknown_platform")
        assert result == "SRE"


# ---------------------------------------------------------------------------
# Task 6 — resolve_url: no-override regression guard
# ---------------------------------------------------------------------------


class TestResolveUrlNoOverride:
    """With no override, resolve_url == extract_field(posting, JOB_URL_FIELDS)."""

    def test_lever_canonical_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_url(LEVER_POSTING, "lever")
        expected = extract_field(LEVER_POSTING, JOB_URL_FIELDS)
        assert result == expected
        assert result == "https://jobs.lever.co/acme/abc-def"

    def test_greenhouse_canonical_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_url(GREENHOUSE_POSTING, "greenhouse")
        expected = extract_field(GREENHOUSE_POSTING, JOB_URL_FIELDS)
        assert result == expected
        assert result == "https://boards.greenhouse.io/acme/jobs/123"

    def test_missing_url_returns_none_no_override(self):
        posting = {"text": "Engineer"}
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_url(posting, "lever")
        assert result is None


# ---------------------------------------------------------------------------
# Task 6 — resolve_title / resolve_url: override extras appended AFTER canonical
# ---------------------------------------------------------------------------


class TestResolveTitleWithOverride:
    """Override adds extra aliases AFTER canonical; canonical-keyed postings still resolve."""

    def test_renamed_key_resolves_with_override(self):
        """A posting using a renamed key (jobTitle) resolves when override provides it."""
        posting = {"jobTitle": "Staff Engineer"}
        recipe = _make_alias_recipe(title_fields=["jobTitle"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_title(posting, "lever")
        assert result == "Staff Engineer"

    def test_canonical_key_still_resolves_with_override(self):
        """A posting using canonical key ('text') still resolves even when override is active.

        Canonical list comes FIRST → first-match-wins on un-renamed data is preserved.
        """
        recipe = _make_alias_recipe(title_fields=["jobTitle"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_title(LEVER_POSTING, "lever")
        # 'text' is in canonical list and comes first — should still win
        assert result == "Backend Engineer"

    def test_canonical_wins_over_override_extra(self):
        """When both canonical key and an override extra key are present, canonical wins."""
        posting = {"text": "Canonical Title", "jobTitle": "Override Title"}
        recipe = _make_alias_recipe(title_fields=["jobTitle"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_title(posting, "lever")
        assert result == "Canonical Title"

    def test_override_with_empty_title_fields_falls_through_to_canonical(self):
        """An override with empty title_fields only looks at canonical."""
        recipe = _make_alias_recipe(url_fields=["jobUrl"])  # title_fields is []
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_title(LEVER_POSTING, "lever")
        assert result == "Backend Engineer"


class TestResolveUrlWithOverride:
    """Override adds extra url aliases AFTER canonical; canonical-keyed postings still resolve."""

    def test_renamed_url_key_resolves_with_override(self):
        """A posting using renamed key (jobUrl) resolves when override provides it."""
        recipe = _make_alias_recipe(url_fields=["jobUrl"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_url(RENAMED_URL_POSTING, "lever")
        assert result == "https://jobs.lever.co/acme/xyz-123"

    def test_canonical_url_still_resolves_with_override(self):
        """A posting using canonical key (hostedUrl) still resolves when override is active."""
        recipe = _make_alias_recipe(url_fields=["jobUrl"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_url(CANONICAL_URL_POSTING, "lever")
        assert result == "https://jobs.lever.co/acme/canon-456"

    def test_canonical_url_wins_over_override_extra(self):
        """When both canonical key and override extra key present, canonical key wins."""
        posting = {"hostedUrl": "https://canonical.url/job", "jobUrl": "https://override.url/job"}
        recipe = _make_alias_recipe(url_fields=["jobUrl"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_url(posting, "lever")
        assert result == "https://canonical.url/job"

    def test_greenhouse_renamed_absolute_url(self):
        """Greenhouse posting with renamed absolute_url resolves via override."""
        posting = {"title": "DevOps Engineer", "jobLink": "https://boards.greenhouse.io/x/1"}
        recipe = _make_alias_recipe("ats:greenhouse", url_fields=["jobLink"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_url(posting, "greenhouse")
        assert result == "https://boards.greenhouse.io/x/1"


# ---------------------------------------------------------------------------
# Task 6 — resolve_job_array: no-override regression guard + override extras
# ---------------------------------------------------------------------------


class TestResolveJobArrayNoOverride:
    """With no override, resolve_job_array == find_job_array(data)."""

    def test_list_data_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_job_array(LEVER_DATA, "lever")
        expected = find_job_array(LEVER_DATA)
        assert result == expected
        assert result == [LEVER_POSTING]

    def test_dict_with_jobs_key_no_override(self):
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_job_array(JOBS_DATA, "greenhouse")
        expected = find_job_array(JOBS_DATA)
        assert result == expected
        assert result == [GREENHOUSE_POSTING]

    def test_no_jobs_found_returns_none_no_override(self):
        data = {"unrecognized_key": []}
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = resolve_job_array(data, "greenhouse")
        assert result is None


class TestResolveJobArrayWithOverride:
    """Override adds extra array_keys AFTER canonical; canonical-keyed data still resolves."""

    def test_renamed_array_key_resolves_with_override(self):
        """A dict using renamed array key (openPositions) resolves when override provides it."""
        recipe = _make_alias_recipe(array_keys=["openPositions"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_job_array(RENAMED_ARRAY_DATA, "greenhouse")
        assert result == [GREENHOUSE_POSTING]

    def test_canonical_array_key_still_resolves_with_override(self):
        """A dict using canonical key (jobs) still resolves when override is active."""
        recipe = _make_alias_recipe(array_keys=["openPositions"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_job_array(JOBS_DATA, "greenhouse")
        assert result == [GREENHOUSE_POSTING]

    def test_canonical_wins_over_override_array_key(self):
        """When both canonical key and override extra are present, canonical wins."""
        data = {"jobs": [GREENHOUSE_POSTING], "openPositions": [LEVER_POSTING]}
        recipe = _make_alias_recipe(array_keys=["openPositions"])
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_job_array(data, "greenhouse")
        assert result == [GREENHOUSE_POSTING]

    def test_override_with_empty_array_keys_canonical_only(self):
        """An override with empty array_keys still resolves via canonical."""
        recipe = _make_alias_recipe(url_fields=["jobUrl"])  # array_keys=[]
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=recipe):
            result = resolve_job_array(JOBS_DATA, "greenhouse")
        assert result == [GREENHOUSE_POSTING]


# ---------------------------------------------------------------------------
# Task 6 — extract_field and find_job_array themselves UNCHANGED
# ---------------------------------------------------------------------------


class TestCanonicalHelpersUnchanged:
    """Verify extract_field and find_job_array are not modified (direct call, no mock)."""

    def test_extract_field_lever_title(self):
        assert extract_field(LEVER_POSTING, JOB_TITLE_FIELDS) == "Backend Engineer"

    def test_extract_field_greenhouse_url(self):
        assert (
            extract_field(GREENHOUSE_POSTING, JOB_URL_FIELDS)
            == "https://boards.greenhouse.io/acme/jobs/123"
        )

    def test_find_job_array_list(self):
        assert find_job_array(LEVER_DATA) == [LEVER_POSTING]

    def test_find_job_array_dict(self):
        assert find_job_array(JOBS_DATA) == [GREENHOUSE_POSTING]


# ---------------------------------------------------------------------------
# Task 7 — _platforms_greenhouse and _platforms_lever: no-override identical output
# ---------------------------------------------------------------------------


class TestGreenhousePlatformNoOverride:
    """With no override, _platforms_greenhouse._posting_to_job output is identical to pre-C2."""

    def _make_posting(self) -> dict:
        return {
            "title": "Staff Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/999",
            "location": {"name": "Remote"},
            "id": 999,
            "content": "<p>Job description</p>",
        }

    def test_title_unchanged_no_override(self):
        from job_finder.web.ats_platforms._platforms_greenhouse import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")
        assert result["title"] == "Staff Engineer"

    def test_url_unchanged_no_override(self):
        from job_finder.web.ats_platforms._platforms_greenhouse import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")
        assert result["source_url"] == "https://boards.greenhouse.io/acme/jobs/999"

    def test_output_identical_to_pre_c2(self):
        """Verify resolve_title/resolve_url with no override == extract_field directly."""
        from job_finder.web.ats_platforms._platforms_greenhouse import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")

        # These are the pre-C2 values
        assert result["title"] == extract_field(posting, JOB_TITLE_FIELDS) or ""
        assert result["source_url"] == extract_field(posting, JOB_URL_FIELDS) or ""


class TestLeverPlatformNoOverride:
    """With no override, _platforms_lever._posting_to_job output is identical to pre-C2."""

    def _make_posting(self) -> dict:
        return {
            "text": "Senior Backend Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/lever-999",
            "id": "lever-999",
            "categories": {"location": "Austin, TX"},
        }

    def test_title_unchanged_no_override(self):
        from job_finder.web.ats_platforms._platforms_lever import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")
        assert result["title"] == "Senior Backend Engineer"

    def test_url_unchanged_no_override(self):
        from job_finder.web.ats_platforms._platforms_lever import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")
        assert result["source_url"] == "https://jobs.lever.co/acme/lever-999"

    def test_output_identical_to_pre_c2(self):
        """Verify resolve_title/resolve_url with no override == extract_field directly."""
        from job_finder.web.ats_platforms._platforms_lever import _posting_to_job

        posting = self._make_posting()
        with patch("job_finder.web.autoheal.override_loader.ats_alias", return_value=None):
            result = _posting_to_job(posting, "acme")

        assert result["title"] == extract_field(posting, JOB_TITLE_FIELDS) or ""
        assert result["source_url"] == extract_field(posting, JOB_URL_FIELDS) or ""


# ---------------------------------------------------------------------------
# Task 7 — careers_page_interactions.py UNTOUCHED check
# ---------------------------------------------------------------------------


class TestCareersPageInteractionsUntouched:
    """Verify careers_page_interactions still uses extract_field (not the resolvers)."""

    def test_careers_page_interactions_uses_extract_field(self):
        """careers_page_interactions must NOT import resolve_title/resolve_url."""
        import ast

        source = Path("job_finder/web/careers_page_interactions.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "_field_alias" in node.module:
                    for alias in node.names:
                        imported_names.add(alias.name)

        # Must not import the new resolvers
        assert "resolve_title" not in imported_names
        assert "resolve_url" not in imported_names
        assert "resolve_job_array" not in imported_names

        # Should still import extract_field (canonical path)
        assert "extract_field" in imported_names
