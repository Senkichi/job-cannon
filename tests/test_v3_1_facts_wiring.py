"""Tests: the v3.1 variant injects the Location facts block into the user message.

End-to-end through ``score_job`` (call_model mocked): under
``prompt_variant=v3_1`` the user message's location line is replaced by the
deterministic ``Location facts: …`` block read from the row's structured
columns; under baseline the legacy ``Location: <string>`` line is byte-identical.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from job_finder.web.job_scorer import score_job
from job_finder.web.model_provider import ModelResult

_CTX = "## Candidate context\n\n### Targeting\n- Target titles: Data Scientist"

_STUB_JD = (
    "We are looking for a Senior Data Scientist to join our team. "
    "You will design, build, and operate ML systems at scale, partnering with "
    "cross-functional teams to ship reliable features end to end. Requirements: "
    "strong Python and SQL, hands-on cloud infrastructure, production observability."
)


def _model_result() -> ModelResult:
    return ModelResult(
        data={
            "title_fit": 4,
            "location_fit": 3,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 4,
            "skills_match": 4,
            "rationale": {
                "strengths": ["x"],
                "gaps": [],
                "talking_points": [],
                "resume_priority_skills": [],
            },
            "legitimacy_note": None,
        },
        cost_usd=0.0,
        input_tokens=10,
        output_tokens=10,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )


def _seed(conn, dedup_key, *, locations_structured, workplace_type, primary_country_code):
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, location, sources,
           source_urls, source_id, first_seen, last_seen, score,
           score_breakdown, user_interest, jd_full,
           locations_structured, workplace_type, primary_country_code)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Data Scientist",
            "EY",
            "",  # empty flat location — facts block must still render from structured
            '["test"]',
            '["https://example.com"]',
            "src-1",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
            0.0,
            "{}",
            "unreviewed",
            _STUB_JD,
            locations_structured,
            workplace_type,
            primary_country_code,
        ),
    )
    conn.commit()


def _user_message(job, conn, config) -> str:
    with patch("job_finder.web.job_scorer.call_model") as mock_call:
        mock_call.return_value = _model_result()
        score_job(job, conn, config, _CTX)
    return mock_call.call_args.kwargs["messages"][0]["content"]


def test_v3_1_injects_facts_block_hyderabad(migrated_db):
    _path, conn = migrated_db
    dedup_key = "ey-hyderabad-v31"
    _seed(
        conn,
        dedup_key,
        locations_structured=json.dumps(
            [
                {
                    "city": "Hyderabad",
                    "region": "Telangana",
                    "country": "India",
                    "country_code": "IN",
                    "workplace_type": "ONSITE",
                    "unresolved": False,
                }
            ]
        ),
        workplace_type="ONSITE",
        primary_country_code="IN",
    )
    job = {
        "dedup_key": dedup_key,
        "title": "Data Scientist",
        "company": "EY",
        "jd_full": _STUB_JD,
        # carried on the job dict in production via JOBS_ALL_COLUMNS — also clears
        # the P3.2 location gate (structured present).
        "locations_structured": json.dumps(
            [{"city": "Hyderabad", "country_code": "IN", "workplace_type": "ONSITE"}]
        ),
    }
    config = {
        "scoring": {"prompt_variant": "v3_1"},
        "profile": {"target_locations": ["Remote"], "home_country": "US"},
        "providers": {"primary": "ollama", "fallback_chain": []},
    }
    msg = _user_message(job, conn, config)
    assert "Location facts:" in msg
    assert "candidate-geography-match=no" in msg  # foreign onsite, remote-only targets
    assert "cities=[Hyderabad]" in msg
    # The legacy free-text location line is replaced, not duplicated.
    assert "\nLocation: " not in msg


def test_baseline_keeps_legacy_location_line(migrated_db):
    _path, conn = migrated_db
    dedup_key = "ey-baseline-loc"
    _seed(
        conn,
        dedup_key,
        locations_structured=json.dumps(
            [
                {
                    "city": "Hyderabad",
                    "country": "India",
                    "country_code": "IN",
                    "workplace_type": "ONSITE",
                    "unresolved": False,
                }
            ]
        ),
        workplace_type="ONSITE",
        primary_country_code="IN",
    )
    job = {
        "dedup_key": dedup_key,
        "title": "Data Scientist",
        "company": "EY",
        "location": "Hyderabad, India",
        "jd_full": _STUB_JD,
    }
    config = {
        "profile": {"target_locations": ["Remote"], "home_country": "US"},
        "providers": {"primary": "ollama", "fallback_chain": []},
    }
    msg = _user_message(job, conn, config)
    assert "Location: Hyderabad, India" in msg
    assert "Location facts:" not in msg


def test_v3_1_remote_match_yes(migrated_db):
    _path, conn = migrated_db
    dedup_key = "remote-v31-yes"
    _seed(
        conn,
        dedup_key,
        locations_structured=json.dumps(
            [
                {
                    "city": None,
                    "country": None,
                    "country_code": None,
                    "workplace_type": "REMOTE",
                    "unresolved": False,
                }
            ]
        ),
        workplace_type="REMOTE",
        primary_country_code=None,
    )
    job = {
        "dedup_key": dedup_key,
        "title": "Data Scientist",
        "company": "EY",
        "jd_full": _STUB_JD,
        "locations_structured": json.dumps([{"workplace_type": "REMOTE"}]),
    }
    config = {
        "scoring": {"prompt_variant": "v3_1"},
        "profile": {"target_locations": ["Remote"], "home_country": "US"},
        "providers": {"primary": "ollama", "fallback_chain": []},
    }
    msg = _user_message(job, conn, config)
    assert "candidate-geography-match=yes" in msg
    assert "workplace=REMOTE" in msg
