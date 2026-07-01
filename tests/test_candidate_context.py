"""Tests for build_candidate_context() — merges config.yaml [profile]
with experience_profile.json into a prompt-ready string.

Phase 2a sub-fix 1/3 (RC1, RC2). Spec D-2.1, D-2.3.
"""

from job_finder.web.scoring_orchestrator import (
    _render_location_targeting,
    build_candidate_context,
)


def _config(profile=None):
    return {"profile": profile or {}}


def _profile(positions=None, skills=None, education=None):
    return {
        "positions": positions or [],
        "skills": skills or [],
        "education": education or [],
        "resume_preferences": {"summary_style": "concise", "emphasis": []},
    }


def test_returns_str_with_targeting_section():
    config = _config(
        {
            "work_arrangement": "hybrid",
            "target_titles": ["Lead Product Analyst", "Staff Data Scientist"],
            "target_locations": ["San Francisco"],
            "min_salary": 150000,
            "industries": ["Healthcare", "SaaS"],
            "exclusions": {"companies": ["Intuit"], "title_keywords": []},
        }
    )
    profile = _profile()
    out = build_candidate_context(config, profile)
    assert isinstance(out, str)
    assert "Lead Product Analyst" in out
    assert "Staff Data Scientist" in out
    assert "Preferred work arrangement: hybrid" in out
    assert "San Francisco" in out
    assert "150,000" in out or "150000" in out
    assert "Healthcare" in out and "SaaS" in out


def test_includes_position_summaries_one_line_each():
    profile = _profile(
        positions=[
            {
                "title": "Lead, Product Analytics & Experimentation",
                "company": "Apree Health",
                "start_date": "Feb 2024",
                "end_date": None,
                "achievements": [
                    "Directed analytics for 5.5M users",
                    "Designed RCT validating 245% lift",
                ],
            },
            {
                "title": "Senior Data Scientist",
                "company": "Acme",
                "start_date": "Jan 2020",
                "end_date": "Feb 2024",
                "achievements": [],
            },
        ]
    )
    out = build_candidate_context(_config(), profile)
    assert "Lead, Product Analytics & Experimentation" in out
    assert "Apree Health" in out
    assert "Senior Data Scientist" in out
    # 1-line per position; no full achievement lists
    assert "245% lift" not in out  # achievements summarized, not enumerated


def test_includes_top_30_skills():
    profile = _profile(skills=[f"skill_{i}" for i in range(40)])
    out = build_candidate_context(_config(), profile)
    assert "skill_0" in out
    assert "skill_29" in out
    assert "skill_30" not in out  # truncated to top 30


def test_handles_empty_profile_and_empty_config():
    out = build_candidate_context({"profile": {}}, _profile())
    assert isinstance(out, str)
    assert len(out) > 0
    assert "Not specified" in out or "No positions" in out


def test_token_budget_under_600():
    """Approximate guard: profile injection should stay under ~600 tokens."""
    config = _config(
        {
            "work_arrangement": "remote",
            "target_titles": [f"Title {i}" for i in range(20)],
            "target_locations": ["SF", "NY"],
            "min_salary": 150000,
            "industries": ["Healthcare", "SaaS", "FinTech"],
            "exclusions": {"companies": [], "title_keywords": []},
        }
    )
    profile = _profile(
        positions=[
            {
                "title": f"Title {i}",
                "company": f"Co {i}",
                "start_date": "2020",
                "end_date": None,
                "achievements": [],
            }
            for i in range(8)
        ],
        skills=[f"skill_{i}" for i in range(40)],
    )
    out = build_candidate_context(config, profile)
    # Rough heuristic: 1 token ~ 4 chars
    assert len(out) <= 2400, f"Profile too long: {len(out)} chars (>~600 tokens)"


# ---------------------------------------------------------------------------
# Location preference hierarchy — regression for the inverted-gap bug where the
# scorer emitted "Remote role, candidate prefers San Francisco location" for a
# remote role because the flat "Target locations: San Francisco, Remote" list
# carried no preference ordering (config had SF listed first).
# ---------------------------------------------------------------------------


def test_remote_preference_declares_remote_ideal_never_a_gap():
    """A remote-preferring candidate must have remote stated as the IDEAL match
    and explicitly disqualified from being a location gap."""
    out = build_candidate_context(
        _config({"work_arrangement": "remote", "target_locations": ["San Francisco", "Remote"]}),
        _profile(),
    )
    low = out.lower()
    assert "ideal location match" in low
    assert "never list a remote role as a location gap" in low
    # SF is present but framed as an on-site/hybrid fallback, not a co-equal
    # target that could be read as the primary preference.
    assert "San Francisco" in out
    assert "acceptable on-site/hybrid geographies" in low


def test_remote_modality_token_stripped_from_geographies():
    """The 'Remote' token is a modality, not a place — it must not appear in the
    rendered geography list (single-sourced with location_fit._target_loc_matches)."""
    lines = _render_location_targeting("remote", ["San Francisco", "Remote"])
    geo_line = next(ln for ln in lines if "geographies" in ln.lower())
    # The geography enumeration lists the real place, not the modality token.
    assert "San Francisco" in geo_line
    assert "Remote" not in geo_line  # 'remote' the place-token is stripped
    # No flat "San Francisco, Remote" enumeration anywhere.
    assert "San Francisco, Remote" not in "\n".join(lines)


def test_no_flat_target_locations_line():
    """The pre-fix flat 'Target locations: <geo>, Remote' rendering that caused
    the inversion must be gone."""
    out = build_candidate_context(
        _config({"work_arrangement": "remote", "target_locations": ["San Francisco", "Remote"]}),
        _profile(),
    )
    assert "Target locations: San Francisco, Remote" not in out


def test_hybrid_and_onsite_render_geographies_as_match():
    """Non-remote arrangements still express geography membership as a match and
    keep remote as a strong fallback (no inversion in either direction)."""
    for wa in ("hybrid", "on-site"):
        lines = _render_location_targeting(wa, ["San Francisco", "Remote"])
        joined = "\n".join(lines).lower()
        assert f"preferred work arrangement: {wa}" in joined
        assert "san francisco" in joined
        assert "remote role is also a strong match" in joined


def test_empty_geographies_uses_not_specified_sentinel():
    lines = _render_location_targeting("remote", [])
    assert any("Not specified" in ln for ln in lines)


def test_falsy_and_blank_geographies_dropped_not_crashed():
    """None / whitespace-only geography entries (a bare '- ' in hand-edited YAML
    yields None under PyYAML) must be dropped, not crash the ", ".join — matching
    location_fit._target_loc_matches, which _norm-coerces every entry."""
    for wa in ("remote", "hybrid", "on-site"):
        # Deliberately malformed input (None / blank) simulating hand-edited YAML.
        lines = _render_location_targeting(wa, ["San Francisco", None, "   ", "Remote"])  # type: ignore[list-item]
        geo_line = next(ln for ln in lines if "geograph" in ln.lower())
        assert "San Francisco" in geo_line
        # No empty tokens rendered ("..., ," or trailing ", ").
        assert ", ," not in geo_line
        assert "None" not in geo_line
