"""Tests for build_candidate_context() — merges config.yaml [profile]
with experience_profile.json into a prompt-ready string.

Phase 2a sub-fix 1/3 (RC1, RC2). Spec D-2.1, D-2.3.
"""

from job_finder.web.scoring_orchestrator import build_candidate_context


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
    assert "Work arrangement: hybrid" in out
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
