"""Dormant ATS-seam regression guard (Phase C / C2).

Verifies that:
1. With NO override present, ``resolve_title`` / ``resolve_url`` /
   ``resolve_job_array`` return exactly what the canonical
   ``extract_field`` / ``find_job_array`` return today (Lever ``text`` /
   ``hostedUrl``, Greenhouse ``title`` / ``absolute_url``).
2. With an override adding a renamed key, postings using the renamed key
   resolve while canonical postings STILL resolve (override extras are
   appended AFTER the canonical list — first-match-wins preserved).
3. The greenhouse/lever platform mappers produce identical job dicts with
   no override present (pre-C2 behaviour).
"""

from __future__ import annotations

import pytest

from job_finder.web._field_alias import (
    JOB_TITLE_FIELDS,
    JOB_URL_FIELDS,
    extract_field,
    find_job_array,
    resolve_job_array,
    resolve_title,
    resolve_url,
)
from job_finder.web.autoheal import override_loader
from job_finder.web.autoheal.override_loader import OverrideLoader

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LEVER_POSTING = {
    "id": "abc-123",
    "text": "Senior Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "categories": {"location": "Remote"},
}

_GREENHOUSE_POSTING = {
    "id": 4567,
    "title": "Staff Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4567",
    "location": {"name": "NYC"},
}


@pytest.fixture
def empty_loader(tmp_path, monkeypatch):
    """Point the module-level singleton at an empty overrides dir."""
    loader = OverrideLoader(overrides_root=tmp_path)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader


@pytest.fixture
def lever_url_override(tmp_path, monkeypatch):
    """Override adding a renamed Lever url key ('jobUrl' is NOT canonical-first)."""
    loader = OverrideLoader(overrides_root=tmp_path)
    loader.write_override(
        "ats",
        "lever",
        {"source": "ats:lever", "title_fields": [], "url_fields": ["renamedUrl"], "array_keys": []},
    )
    loader.reload()
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader


# ---------------------------------------------------------------------------
# Task 6 — resolvers, no override: identical to canonical extract_field
# ---------------------------------------------------------------------------


def test_resolve_title_no_override_matches_extract_field_lever(empty_loader):
    assert resolve_title(_LEVER_POSTING, "lever") == extract_field(
        _LEVER_POSTING, JOB_TITLE_FIELDS
    )
    assert resolve_title(_LEVER_POSTING, "lever") == "Senior Engineer"


def test_resolve_url_no_override_matches_extract_field_lever(empty_loader):
    assert resolve_url(_LEVER_POSTING, "lever") == extract_field(_LEVER_POSTING, JOB_URL_FIELDS)
    assert resolve_url(_LEVER_POSTING, "lever") == "https://jobs.lever.co/acme/abc-123"


def test_resolve_title_no_override_matches_extract_field_greenhouse(empty_loader):
    assert resolve_title(_GREENHOUSE_POSTING, "greenhouse") == extract_field(
        _GREENHOUSE_POSTING, JOB_TITLE_FIELDS
    )
    assert resolve_title(_GREENHOUSE_POSTING, "greenhouse") == "Staff Engineer"


def test_resolve_url_no_override_matches_extract_field_greenhouse(empty_loader):
    assert resolve_url(_GREENHOUSE_POSTING, "greenhouse") == extract_field(
        _GREENHOUSE_POSTING, JOB_URL_FIELDS
    )


def test_resolve_job_array_no_override_matches_find_job_array(empty_loader):
    data = {"jobs": [{"title": "X"}]}
    assert resolve_job_array(data, "lever") == find_job_array(data)
    assert resolve_job_array({"unrecognized": []}, "lever") is None


# ---------------------------------------------------------------------------
# Task 6 — with override: renamed key resolves, canonical still wins
# ---------------------------------------------------------------------------


def test_override_renamed_url_key_resolves(lever_url_override):
    renamed = {"text": "Eng", "renamedUrl": "https://example.com/renamed"}
    assert resolve_url(renamed, "lever") == "https://example.com/renamed"


def test_override_canonical_posting_still_resolves(lever_url_override):
    # Un-renamed posting must be untouched by the override
    assert resolve_url(_LEVER_POSTING, "lever") == "https://jobs.lever.co/acme/abc-123"


def test_override_canonical_wins_when_both_present(lever_url_override):
    both = {"hostedUrl": "https://canonical", "renamedUrl": "https://override"}
    assert resolve_url(both, "lever") == "https://canonical"


def test_override_does_not_leak_to_other_platform(lever_url_override):
    renamed = {"title": "Eng", "renamedUrl": "https://example.com/renamed"}
    # Override is keyed ats:lever — greenhouse must not see it
    assert resolve_url(renamed, "greenhouse") is None


def test_override_array_keys_consulted_after_canonical(tmp_path, monkeypatch):
    loader = OverrideLoader(overrides_root=tmp_path)
    loader.write_override(
        "ats",
        "lever",
        {"source": "ats:lever", "title_fields": [], "url_fields": [], "array_keys": ["vacancies"]},
    )
    loader.reload()
    monkeypatch.setattr(override_loader, "_LOADER", loader)

    postings = [{"text": "Eng", "hostedUrl": "u"}]
    assert resolve_job_array({"vacancies": postings}, "lever") == postings
    # Canonical keys still resolve first
    assert resolve_job_array({"jobs": postings}, "lever") == postings
    # Nested under a known outer key also resolves
    assert resolve_job_array({"data": {"vacancies": postings}}, "lever") == postings


# ---------------------------------------------------------------------------
# Task 7 — greenhouse/lever mappers unchanged with no override
# ---------------------------------------------------------------------------


def test_lever_posting_to_job_canonical_unchanged(empty_loader):
    from job_finder.web.ats_platforms._platforms_lever import _posting_to_job

    job = _posting_to_job(_LEVER_POSTING, "acme")
    assert job["title"] == "Senior Engineer"
    assert job["source_url"] == "https://jobs.lever.co/acme/abc-123"


def test_greenhouse_posting_to_job_canonical_unchanged(empty_loader):
    from job_finder.web.ats_platforms._platforms_greenhouse import _posting_to_job

    job = _posting_to_job(_GREENHOUSE_POSTING, "acme")
    assert job["title"] == "Staff Engineer"
    assert job["source_url"] == "https://boards.greenhouse.io/acme/jobs/4567"


def test_lever_posting_to_job_resolves_renamed_key_via_override(lever_url_override):
    from job_finder.web.ats_platforms._platforms_lever import _posting_to_job

    renamed = {"id": "x", "text": "Eng", "renamedUrl": "https://example.com/renamed"}
    job = _posting_to_job(renamed, "acme")
    assert job["source_url"] == "https://example.com/renamed"
