"""Tests for resume grounding validator (issue #600)."""

from pathlib import Path

import pytest

from job_finder.web.profile_schema import load_profile
from job_finder.web.resume_grounding import validate_resume_grounding
from job_finder.web.resume_tailor import load_style_guide

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def example_profile():
    """Load the example profile for testing."""
    profile_path = Path(__file__).parent.parent / "experience_profile.example.json"
    return load_profile(str(profile_path))


@pytest.fixture
def base_job():
    """Base job dict for testing."""
    return {
        "dedup_key": "test-job",
        "company": "TargetCorp",
        "title": "Data Scientist",
        "location": "San Francisco, CA",
        "jd_full": "We are looking for a Data Scientist with Python and SQL experience.",
    }


@pytest.fixture
def style_guide():
    """Load the style guide for testing."""
    return load_style_guide()


# ---------------------------------------------------------------------------
# Test: Layer A - structural subset (fabricated facts)
# ---------------------------------------------------------------------------


def test_validator_catches_fabricated_employer(example_profile, base_job, style_guide):
    """Layer A: Fabricated employer is caught."""
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "ACME-FABRICATED-CORP",  # Not in profile
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            },
            {
                "company": "TechCorp Solutions",  # Real company
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            },
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    # Should catch fabricated company
    assert len(report.violations) >= 1
    company_violations = [v for v in report.violations if v.kind == "company"]
    assert len(company_violations) >= 1
    assert "ACME-FABRICATED-CORP" in company_violations[0].value


def test_validator_catches_fabricated_title(example_profile, base_job):
    """Layer A: Fabricated title is caught."""
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",  # Real company
                "title": "Chief Imaginary Officer",  # Not in profile
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch fabricated title
    title_violations = [v for v in report.violations if v.kind == "title"]
    assert len(title_violations) >= 1
    assert "Chief Imaginary Officer" in title_violations[0].value


def test_validator_catches_fabricated_skills(example_profile, base_job):
    """GROUP D1: Fabricated skills are caught.

    Every tailored skill must be grounded in the profile (profile-level or position-level).
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": [
            "Python",  # Grounded
            "Quantum Cryptography",  # Fabricated
            "AWS Certified Solutions Architect",  # Fabricated
        ],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch fabricated skills
    skill_violations = [v for v in report.violations if v.kind == "skill"]
    assert len(skill_violations) >= 2  # Both fabricated skills
    violation_values = [v.value for v in skill_violations]
    assert "Quantum Cryptography" in violation_values
    assert "AWS Certified Solutions Architect" in violation_values


def test_validator_catches_invented_year(example_profile, base_job):
    """Layer A: Invented year is caught."""
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",  # Real company
                "title": "Senior Data Scientist",
                "dates": "Mar 2025 - Present",  # 2025 not in profile (profile has 2022)
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch invented year
    date_violations = [v for v in report.violations if v.kind == "dates"]
    assert len(date_violations) >= 1
    assert "2025" in date_violations[0].value


def test_validator_catches_invented_year_with_blank_company(example_profile, base_job):
    """GROUP C1: Invented years with blank company are caught.

    When company is blank, years should be validated against the union of all
    profile employment years.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "",  # Blank company
                "title": "Senior Data Scientist",
                "dates": "2008 - 2011",  # Years not in profile (profile has 2019-2022)
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch invented years
    date_violations = [v for v in report.violations if v.kind == "dates"]
    assert len(date_violations) >= 1


def test_validator_catches_invented_year_with_ungrounded_company(example_profile, base_job):
    """GROUP C1: Invented years with ungrounded company are caught.

    When company is fabricated, years should still be validated against the union
    of all profile employment years (in addition to the company violation).
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "FakeCo",  # Fabricated company
                "title": "Senior Data Scientist",
                "dates": "2010 - 2015",  # Years not in profile
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch both company violation and year violations
    company_violations = [v for v in report.violations if v.kind == "company"]
    date_violations = [v for v in report.violations if v.kind == "dates"]
    assert len(company_violations) >= 1
    assert len(date_violations) >= 1


def test_validator_omission_allowed(example_profile, base_job):
    """Layer A: Omitting a true profile fact is allowed (no violation)."""
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
            # Deliberately omit DataDriven Analytics position
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Omission is allowed - no violations
    assert len(report.violations) == 0


def test_validator_catches_company_suffix_false_positive(example_profile, base_job):
    """GROUP A1: Company grounding uses exact string, not dedup normalizer.

    'TechCorp Holdings' should NOT match 'TechCorp Solutions' even though
    the dedup normalizer strips suffixes and would normalize both to 'techcorp'.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Holdings",  # NOT in profile (only 'TechCorp Solutions')
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch fabricated company (suffix difference matters for grounding)
    company_violations = [v for v in report.violations if v.kind == "company"]
    assert len(company_violations) >= 1
    assert "TechCorp Holdings" in company_violations[0].value


# ---------------------------------------------------------------------------
# Test: Layer B - prohibited items
# ---------------------------------------------------------------------------


def test_validator_catches_prohibited_items(example_profile, base_job):
    """Layer B: All mechanized hard-stops are caught."""
    from job_finder.web.resume_tailor import load_style_guide

    style_guide = load_style_guide()

    tailored = {
        "summary": "Experienced data scientist at TargetCorp",  # Company name in summary
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Built ML pipeline using dbt",  # dbt prohibited
                    "Achieved 454% ROI",  # 454% prohibited
                    "Processed N=200 samples",  # N= prohibited
                    "Reduced latency — by 40%",  # em-dash prohibited
                    "5 years of experience",  # sub-8 years prohibited
                ],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    # Should catch multiple prohibited items
    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 5  # dbt, 454%, N=, em-dash, sub-8 years

    # Check specific items
    violation_values = [v.value for v in prohibited_violations]
    assert any("dbt" in val.lower() for val in violation_values)
    assert any("454" in val for val in violation_values)
    assert any("N=" in val for val in violation_values)
    assert any("dash" in val for val in violation_values)  # Changed from em-dash to generic dash
    # GROUP E6: Explicitly pin sub-8-year trigger
    assert any("5 years" in val for val in violation_values)


def test_validator_catches_spark_prohibited(example_profile, base_job):
    """Layer B: Apache Spark is caught (from style_guide banned_tokens)."""
    from job_finder.web.resume_tailor import load_style_guide

    style_guide = load_style_guide()

    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL", "Apache Spark"],  # Spark prohibited
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1
    assert any("spark" in v.value.lower() for v in prohibited_violations)


def test_validator_catches_third_person_without_literal_spaces(example_profile, base_job):
    """GROUP E1: Third-person pronouns caught without literal space requirement.

    'His leadership...' (start) and 'he.' (punctuation) should be caught.
    """
    tailored = {
        "summary": "His leadership drove growth.",  # Start of sentence
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["He delivered results."],  # Punctuation
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1


def test_validator_catches_owner_name_third_person(example_profile, base_job):
    """GROUP E2: Owner's first name from contact.full_name is caught in third-person.

    If profile has contact.full_name='Jordan Lee', 'Jordan delivered...' should violate.
    """
    profile_with_contact = example_profile.copy()
    profile_with_contact["contact"] = {"full_name": "Jordan Lee"}

    tailored = {
        "summary": "Jordan delivered the model.",  # Third-person use of owner's name
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, profile_with_contact, base_job)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1
    assert any("jordan" in v.value.lower() for v in prohibited_violations)


def test_validator_catches_en_dash_in_full_text(example_profile, base_job):
    """GROUP E3: En-dash (–) in full text is caught, not just em-dash in bullets.

    The dash family [‒–—―] should be caught anywhere in the full text.
    """
    tailored = {
        "summary": "Experienced data scientist with 5–10 years experience",  # En-dash in summary
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1


def test_validator_catches_all_sub_8_year_experience_claims(
    example_profile, base_job, style_guide
):
    """Issue #624: All sub-8-year total-career experience claims are caught.

    '12 years of leadership; 3 years of experience' should catch the 3 years violation
    because it's a total-career self-claim, but '3 years of Python' should NOT be caught
    because it's a role/project duration.
    """
    tailored = {
        "summary": "12 years of leadership; 3 years of experience",  # 3 years should be caught
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    # Should catch the 3 years violation (total-career claim)
    assert any("3 years" in v.value for v in prohibited_violations)


def test_validator_454_anchored_to_percent(example_profile, base_job):
    """GROUP E5: 454% is caught only when anchored to percent sign.

    '454' alone should not trigger (e.g., in a version number), but '454%' should.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Achieved 454% ROI"],  # Should trigger
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert any("454%" in v.value for v in prohibited_violations)


def test_min_years_anchor_blocks_under_claimed_total_career(
    example_profile, base_job, style_guide
):
    """Issue #624: Under-claimed total-career experience is still blocked.

    '5 years of experience' with min_years_anchor=8 should still violate
    (preserves the owner's understatement guard).
    """
    tailored = {
        "summary": "5 years of experience in data science",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert any("5 years" in v.value for v in prohibited_violations)


def test_min_years_anchor_allows_truthful_role_duration(example_profile, base_job, style_guide):
    """Issue #624: Truthful role duration below floor is allowed (false-positive fix).

    'Led the analytics team for 4 years' with min_years_anchor=8 should NOT violate
    because it's a role duration, not a total-career self-claim.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Led the analytics team for 4 years"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    # Should NOT have a years violation
    assert not any("years" in v.value for v in prohibited_violations)


def test_min_years_anchor_allows_single_role_tenure(example_profile, base_job, style_guide):
    """Issue #624: Truthful single-role tenure below floor is allowed.

    '2 years at [company]' with min_years_anchor=8 should NOT violate
    because it's a role tenure, not a total-career self-claim.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Spent 2 years at TechCorp building ML systems"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    # Should NOT have a years violation
    assert not any("years" in v.value for v in prohibited_violations)


def test_min_years_anchor_allows_above_floor_claim(example_profile, base_job, style_guide):
    """Issue #624: Above-floor total-career claim is allowed.

    '10 years of experience' with min_years_anchor=8 should NOT violate.
    """
    tailored = {
        "summary": "10 years of professional experience in data science",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    # Should NOT have a years violation
    assert not any("years" in v.value for v in prohibited_violations)


def test_min_years_anchor_other_prohibited_checks_unchanged(
    example_profile, base_job, style_guide
):
    """Issue #624: Other prohibited-item checks remain unaffected.

    Verify that banned_tokens, company-name-in-summary, sample sizes, em-dash,
    ROI %, and third-person checks still work after the min_years_anchor fix.
    """
    tailored = {
        "summary": "Experienced data scientist at TargetCorp with 5 years of experience",
        "skills": ["Python", "SQL", "dbt"],  # dbt prohibited
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Achieved 454% ROI",  # 454% prohibited
                    "Processed N=200 samples",  # N= prohibited
                    "Reduced latency — by 40%",  # em-dash prohibited
                    "He delivered results.",  # third-person prohibited
                ],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    violation_values = [v.value for v in prohibited_violations]

    # Should catch all other prohibited items
    assert any("dbt" in val.lower() for val in violation_values)
    assert any("TargetCorp" in val for val in violation_values)
    assert any("454%" in val for val in violation_values)
    assert any("N=" in val for val in violation_values)
    assert any("dash" in val for val in violation_values)
    assert any("third-person" in val for val in violation_values)
    # Should also catch the sub-8-year total-career claim
    assert any("5 years" in val for val in violation_values)


# ---------------------------------------------------------------------------
# Test: Layer C - title-alignment allowlist
# ---------------------------------------------------------------------------


def test_validator_catches_unlisted_title(example_profile, base_job):
    """Layer C: Unlisted most-recent title is caught."""
    # Most-recent position has title_variants: ["Lead Data Scientist", "Analytics Lead", "Machine Learning Lead"]
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Distinguished AI Architect",  # NOT in {canonical} ∪ variants
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch unlisted title
    title_violations = [v for v in report.violations if v.kind == "title_unlisted"]
    assert len(title_violations) >= 1
    assert "Distinguished AI Architect" in title_violations[0].value


def test_validator_allows_declared_variant(example_profile, base_job):
    """Layer C: Declared variant title is allowed."""
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Analytics Lead",  # In title_variants
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Declared variant should pass
    assert len(report.violations) == 0


def test_validator_failsafe_missing_variants(example_profile, base_job):
    """Layer C: Missing title_variants collapses admissible set to {canonical}."""
    # Create a profile copy without title_variants
    profile_no_variants = example_profile.copy()
    profile_no_variants["positions"] = [p.copy() for p in example_profile["positions"]]
    profile_no_variants["positions"][0].pop("title_variants", None)

    # Canonical title should pass
    tailored_canonical = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",  # Canonical
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored_canonical, profile_no_variants, base_job)
    assert len(report.violations) == 0  # Canonical passes

    # Previously legal variant should now fail
    tailored_variant = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Analytics Lead",  # Previously legal, now unlisted
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored_variant, profile_no_variants, base_job)
    title_violations = [v for v in report.violations if v.kind == "title_unlisted"]
    assert len(title_violations) >= 1


def test_validator_catches_title_transplant_across_companies(example_profile, base_job):
    """GROUP B1: Per-company title validation catches title transplant.

    'Machine Learning Lead' is a TechCorp-only variant. Using it on a DataDriven
    section should violate per-company title validation.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "DataDriven Analytics",  # This company
                "title": "Machine Learning Lead",  # TechCorp-only variant
                "dates": "Jun 2019 - Feb 2022",
                "bullets": ["Built ML pipeline"],
            },
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            },
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch title violation (Machine Learning Lead not in DataDriven's allowlist)
    title_violations = [v for v in report.violations if v.kind == "title"]
    assert len(title_violations) >= 1
    assert "Machine Learning Lead" in title_violations[0].value


def test_validator_catches_recency_swap_title_transplant(example_profile, base_job):
    """GROUP B2/B3: Recency-swap title transplant is caught.

    If sections are reordered such that the most-recent TechCorp position
    carries DataDriven's canonical title, it should violate.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "DataDriven Analytics",
                "title": "Senior Data Scientist",  # TechCorp's canonical (wrong company)
                "dates": "Jun 2019 - Feb 2022",
                "bullets": ["Built ML pipeline"],
            },
            {
                "company": "TechCorp Solutions",
                "title": "Data Scientist",  # DataDriven's canonical (wrong company)
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            },
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch title violations (both titles are transplanted)
    title_violations = [v for v in report.violations if v.kind == "title"]
    assert len(title_violations) >= 1


def test_validator_catches_variant_on_wrong_company_section(example_profile, base_job):
    """GROUP B3: Variant on wrong company section is caught.

    'Analytics Lead' is a TechCorp variant. Using it on a DataDriven section
    (even if sections[0]) should violate per-company validation.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "DataDriven Analytics",
                "title": "Analytics Lead",  # TechCorp variant on wrong company
                "dates": "Jun 2019 - Feb 2022",
                "bullets": ["Built ML pipeline"],
            },
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            },
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch title violation
    title_violations = [v for v in report.violations if v.kind == "title"]
    assert len(title_violations) >= 1
    assert "Analytics Lead" in title_violations[0].value


def test_validator_tiebreaks_concurrent_current_roles_by_start_date(example_profile, base_job):
    """GROUP B4: Concurrent current roles tiebreak by start_date, not document order.

    This test is now superseded by test_validator_tiebreaks_concurrent_current_roles_discriminating
    which provides a discriminating test that actually depends on the tiebreak logic.
    """
    # This test is kept for backward compatibility but the discriminating test is better
    profile_concurrent = example_profile.copy()
    profile_concurrent["positions"] = [
        {
            "title": "Engineer",
            "company": "OldCo",
            "start_date": "Jan 2018",  # Earlier start
            "end_date": None,  # Current
            "title_variants": ["Director of Engineering"],  # OldCo-only variant
            "achievements": [],
            "skills": [],
        },
        {
            "title": "Analyst",
            "company": "NewCo",
            "start_date": "Jan 2023",  # Later start → most-recent
            "end_date": None,  # Current
            "title_variants": [],  # No variants
            "achievements": [],
            "skills": [],
        },
    ]

    tailored = {
        "summary": "Experienced analyst",
        "skills": ["Python"],
        "sections": [
            {
                "company": "NewCo",  # True most-recent by start_date
                "title": "Director of Engineering",  # OldCo-only variant
                "dates": "Jan 2023 - Present",
                "bullets": ["Built systems"],
            }
        ],
        "jd_keywords": ["Python"],
    }

    report = validate_resume_grounding(tailored, profile_concurrent, base_job)

    # Should catch title violation (Director of Engineering not in NewCo's allowlist)
    title_violations = [v for v in report.violations if v.kind in ("title", "title_unlisted")]
    assert len(title_violations) >= 1


# ---------------------------------------------------------------------------
# Test: Layer D - keyword coverage (reported, not gated)
# ---------------------------------------------------------------------------


def test_keyword_coverage_is_reported_not_gated(example_profile, base_job):
    """Layer D: Coverage is reported, never a refusal reason."""
    tailored = {
        "summary": "Experienced data scientist with Python and SQL expertise",
        "skills": ["Python", "SQL", "Machine Learning"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline using Python"],
            }
        ],
        "jd_keywords": ["Python", "SQL", "Kubernetes"],  # Kubernetes is an honest gap
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Missing keyword is NOT a fabrication fail
    assert len(report.violations) == 0

    # Coverage metric is computed correctly
    assert report.coverage.ratio == 2 / 3
    assert "Kubernetes" in report.coverage.missing
    assert "Python" in report.coverage.present
    assert "SQL" in report.coverage.present


def test_coverage_cannot_be_inflated(example_profile, base_job):
    """Layer D: Coverage cannot be inflated by fabricated facts."""
    # Try to inflate coverage by adding a fabricated company with Kubernetes
    tailored = {
        "summary": "Experienced data scientist with Python and SQL expertise",
        "skills": ["Python", "SQL", "Kubernetes"],
        "sections": [
            {
                "company": "ACME-FABRICATED-CORP",  # Fabricated company
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline using Python and Kubernetes"],
            }
        ],
        "jd_keywords": ["Python", "SQL", "Kubernetes"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Layer A should catch the fabricated company
    # (even though coverage would be higher for Kubernetes)
    assert len(report.violations) > 0
    company_violations = [v for v in report.violations if v.kind == "company"]
    assert len(company_violations) >= 1


# ---------------------------------------------------------------------------
# Test: Faithful reordered resume passes
# ---------------------------------------------------------------------------


def test_validator_passes_faithful_reordered_resume(example_profile, base_job):
    """A faithful-but-reworded clean resume passes all checks."""
    tailored = {
        "summary": "Senior data scientist with expertise in ML pipelines and A/B testing",
        "skills": ["Python", "SQL", "Machine Learning", "A/B Testing"],  # Reordered
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Analytics Lead",  # Declared variant
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Reduced fraud detection latency by 40% via ML pipeline",  # Reworded
                    "Increased user engagement by 25% with recommendation engine",
                ],
            },
            {
                "company": "DataDriven Analytics",
                "title": "Data Scientist",
                "dates": "Jun 2019 - Feb 2022",
                "bullets": [
                    "Saved $1.2M annually via churn prediction model",  # Reworded
                ],
            },
        ],
        "jd_keywords": ["Python", "SQL", "A/B Testing"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # All checks should pass
    assert len(report.violations) == 0
    assert report.coverage.ratio == 1.0  # All keywords covered


# ---------------------------------------------------------------------------
# Test: Integration with resume_tailor
# ---------------------------------------------------------------------------


def test_tailor_resume_raises_on_fabrication_title_and_prohibited(example_profile, base_job):
    """Integration: tailor_resume raises on violations."""
    from unittest.mock import Mock, patch

    from job_finder.web.resume_tailor import FabricationError, tailor_resume

    # Mock call_model to return a fabricated resume
    mock_result = Mock()
    mock_result.data = {
        "summary": "Experienced data scientist at TargetCorp",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "ACME-FABRICATED-CORP",  # Fabricated employer
                "title": "Distinguished AI Architect",  # Unlisted title
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline using dbt"],  # Prohibited item
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    # Mock prose adjudicator to return no fabrications (focus on deterministic violations)
    mock_prose_result = Mock()
    mock_prose_result.data = {"fabrications": []}

    config = {}
    conn = Mock()

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
        with patch("job_finder.web.resume_grounding.adjudicate_resume_prose", return_value=()):
            with pytest.raises(FabricationError) as exc_info:
                tailor_resume(base_job, example_profile, config, conn)

        # Check that all three violations are present
        violations = exc_info.value.violations
        violation_kinds = {v.kind for v in violations}

        assert "company" in violation_kinds  # Fabricated employer
        assert "title" in violation_kinds  # Fabricated title (not in any profile position)
        assert "prohibited_item" in violation_kinds  # dbt


def test_tailor_resume_returns_clean_resume(example_profile, base_job):
    """Integration: tailor_resume returns a clean resume with coverage metric."""
    from unittest.mock import Mock, patch

    from job_finder.web.resume_tailor import tailor_resume

    # Mock call_model to return a clean resume
    mock_result = Mock()
    mock_result.data = {
        "summary": "Senior data scientist with ML expertise",
        "skills": ["Python", "SQL", "Machine Learning"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    config = {}
    conn = Mock()

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
        with patch("job_finder.web.resume_grounding.adjudicate_resume_prose", return_value=()):
            result = tailor_resume(base_job, example_profile, config, conn)

        # Should return normally with coverage metric
        assert "keyword_coverage" in result
        assert result["keyword_coverage"]["ratio"] == 1.0


def test_validator_catches_fabricated_education(example_profile, base_job):
    """GROUP F1: Fabricated degree/institution are caught.

    The degree/institution detection works; this test adds coverage for it.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "education": [
            {
                "degree": "PhD in Astrophysics",  # Not in profile (only M.S. and B.S.)
                "institution": "Fake University",  # Not in profile
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch both degree and institution violations
    degree_violations = [v for v in report.violations if v.kind == "degree"]
    institution_violations = [v for v in report.violations if v.kind == "institution"]
    assert len(degree_violations) >= 1
    assert len(institution_violations) >= 1


# ---------------------------------------------------------------------------
# Test: Prose adjudicator (GROUP A)
# ---------------------------------------------------------------------------


def test_prose_adjudicator_catches_fabricated_aws_claims(example_profile, base_job):
    """GROUP A3: Prose adjudicator catches fabricated AWS/Principal Engineer claims.

    The exact review input: summary says "Previously Principal Engineer at Amazon Web Services"
    and bullets claim "Led a 50-person org at Amazon Web Services", "Hold an active AWS
    Solutions Architect Professional certification", "Production experience with Kubernetes,
    Terraform, and Rust". These are NOT in the profile and should be flagged.
    """
    from unittest.mock import Mock, patch

    from job_finder.web.resume_grounding import adjudicate_resume_prose

    tailored = {
        "summary": "Previously Principal Engineer at Amazon Web Services",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Led a 50-person org at Amazon Web Services",
                    "Hold an active AWS Solutions Architect Professional certification",
                    "Production experience with Kubernetes, Terraform, and Rust",
                ],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    config = {}
    conn = Mock()

    # Mock call_model to return fabrications
    mock_result = Mock()
    mock_result.data = {
        "fabrications": [
            {
                "claim": "Previously Principal Engineer at Amazon Web Services",
                "type": "job_title",
                "section": "summary",
            },
            {
                "claim": "Led a 50-person org at Amazon Web Services",
                "type": "employer",
                "section": "sections[0]",
            },
            {
                "claim": "Hold an active AWS Solutions Architect Professional certification",
                "type": "certification",
                "section": "sections[0]",
            },
            {
                "claim": "Production experience with Kubernetes, Terraform, and Rust",
                "type": "tool_skill",
                "section": "sections[0]",
            },
        ]
    }

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
        violations = adjudicate_resume_prose(tailored, example_profile, config, conn)

    # Should catch all fabrications
    assert len(violations) >= 4
    assert all(v.kind == "prose_fabrication" for v in violations)


def test_prose_adjudicator_fails_closed_on_empty_claim(example_profile, base_job):
    """GROUP A3: a flagged fabrication with an empty/whitespace claim is schema-valid but
    must NOT be silently dropped. The adjudicator fails closed (adjudicator_unavailable)
    rather than returning a clean pass — otherwise a model that blanks or truncates the
    claim text (e.g. max_tokens) turns a flagged fabrication into a silent pass.
    """
    from unittest.mock import Mock, patch

    from job_finder.web.resume_grounding import adjudicate_resume_prose

    tailored = {
        "summary": "Previously Principal Engineer at Amazon Web Services",
        "skills": ["Python"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipelines"],
            }
        ],
        "jd_keywords": ["Python"],
    }

    # Schema-valid response whose flagged item has a blank claim (only the locator survived).
    mock_result = Mock()
    mock_result.data = {"fabrications": [{"claim": "", "type": "job_title", "section": "summary"}]}

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
        violations = adjudicate_resume_prose(tailored, example_profile, {}, Mock())

    # Fail closed: a blocking adjudicator_unavailable violation, never a clean pass ().
    assert len(violations) >= 1
    assert any(v.kind == "adjudicator_unavailable" for v in violations)


def test_prose_adjudicator_passes_clean_prose(example_profile, base_job):
    """GROUP A3: Prose adjudicator passes when all claims are grounded in achievements.

    Clean prose (all claims grounded in achievements[]/skills/education) → adjudicator
    returns none → resume returned.
    """
    from unittest.mock import Mock, patch

    from job_finder.web.resume_grounding import adjudicate_resume_prose

    tailored = {
        "summary": "Senior data scientist with expertise in ML pipelines and A/B testing",
        "skills": ["Python", "SQL", "Machine Learning"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Built ML pipeline processing 2M+ daily transactions",
                    "Led cross-functional team of 4 to ship recommendation engine",
                ],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    config = {}
    conn = Mock()

    # Mock call_model to return no fabrications
    mock_result = Mock()
    mock_result.data = {"fabrications": []}

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
        violations = adjudicate_resume_prose(tailored, example_profile, config, conn)

    # Should pass with no violations
    assert len(violations) == 0


def test_prose_adjudicator_fails_closed_on_provider_error(example_profile, base_job):
    """GROUP A3: Prose adjudicator fails closed on provider error.

    Provider error (call_model raises) → FAIL CLOSED → FabricationError
    (NOT a silent pass).
    """
    from unittest.mock import Mock, patch

    from job_finder.web.resume_grounding import adjudicate_resume_prose

    tailored = {
        "summary": "Senior data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    config = {}
    conn = Mock()

    # Mock call_model to raise an exception
    with patch(
        "job_finder.web.model_provider.call_model", side_effect=Exception("Provider error")
    ):
        violations = adjudicate_resume_prose(tailored, example_profile, config, conn)

    # Should return adjudicator_unavailable violation (fail-closed)
    assert len(violations) == 1
    assert violations[0].kind == "adjudicator_unavailable"


# ---------------------------------------------------------------------------
# Test: Layer C fallback-to-index-0 fix (GROUP B)
# ---------------------------------------------------------------------------


def test_layer_c_allows_older_only_faithful_subset(example_profile, base_job):
    """GROUP B1: Layer C allows older-only faithful subset (no fallback to index 0).

    When the tailored resume DROPS the most-recent role and keeps only an OLDER role
    (legit when tailoring for a role closer to the earlier job), the validator should
    NOT fall back to index 0. It should validate each section's title against ITS OWN
    company's allowlist.

    Test: tailored = single section {company:'DataDriven Analytics', title:'Data Scientist'
    (its true canonical), dates:'2019 - 2022'}, example profile → NO violation.
    """
    tailored = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "DataDriven Analytics",  # Older company (not most-recent)
                "title": "Data Scientist",  # Its true canonical title
                "dates": "Jun 2019 - Feb 2022",
                "bullets": ["Built ML pipeline"],
            }
            # Deliberately omit TechCorp Solutions (most-recent)
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should pass with no violations (title is canonical for that company)
    assert len(report.violations) == 0


# ---------------------------------------------------------------------------
# Test: Month-aware concurrent-role tiebreak (GROUP C)
# ---------------------------------------------------------------------------


def test_month_aware_concurrent_role_tiebreak(example_profile, base_job):
    """GROUP C1: Month-aware concurrent-role tiebreak.

    Profile has two concurrent MegaCorp roles — {'Junior Analyst','Jan 2023', end None,
    variants:['Trainee']} and {'Principal','Nov 2023', end None, no variants}. Tailored
    MegaCorp section titled 'Trainee' dated 'Nov 2023 - Present' → violation (Nov 2023
    Principal is the true most-recent; 'Trainee' is not in its allowlist).
    """
    # Create profile with two concurrent current roles at same company
    profile_concurrent = example_profile.copy()
    profile_concurrent["positions"] = [
        {
            "title": "Junior Analyst",
            "company": "MegaCorp",
            "start_date": "Jan 2023",  # Earlier start
            "end_date": None,  # Current
            "title_variants": ["Trainee"],  # Junior-only variant
            "achievements": [],
            "skills": [],
        },
        {
            "title": "Principal",
            "company": "MegaCorp",
            "start_date": "Nov 2023",  # Later start → true most-recent
            "end_date": None,  # Current
            "title_variants": [],  # No variants
            "achievements": [],
            "skills": [],
        },
    ]

    tailored = {
        "summary": "Experienced analyst",
        "skills": ["Python"],
        "sections": [
            {
                "company": "MegaCorp",
                "title": "Trainee",  # Junior-only variant on Principal section
                "dates": "Nov 2023 - Present",
                "bullets": ["Built systems"],
            }
        ],
        "jd_keywords": ["Python"],
    }

    report = validate_resume_grounding(tailored, profile_concurrent, base_job)

    # Should catch title violation (Trainee not in Principal's allowlist)
    title_violations = [v for v in report.violations if v.kind in ("title", "title_unlisted")]
    assert len(title_violations) >= 1


# ---------------------------------------------------------------------------
# Test: Discriminating tiebreak test (GROUP D)
# ---------------------------------------------------------------------------


def test_validator_tiebreaks_concurrent_current_roles_discriminating(
    example_profile, base_job, monkeypatch
):
    """GROUP D1: Discriminating tiebreak test — the OUTCOME depends on the month-aware tiebreak.

    Only SAME-company concurrent roles can discriminate the tiebreak: Layer C validates each
    section against its own company's allowlist, so a different-company section is graded
    independently of which role is "most recent". Here the tailored section carries the TRUE
    most-recent role's own title ('Principal'), grounded only when the tiebreak correctly picks
    the later-starting role. The embedded revert proves the outcome flips, so this test cannot
    pass if the tiebreak is broken OR non-discriminating.
    """
    import job_finder.web.resume_grounding as rg

    profile_concurrent = example_profile.copy()
    profile_concurrent["positions"] = [
        {
            "title": "Junior Analyst",
            "company": "MegaCorp",
            "start_date": "Jan 2023",  # Earlier start
            "end_date": None,  # Current
            "title_variants": ["Trainee"],  # Junior-only variant
            "achievements": [],
            "skills": [],
        },
        {
            "title": "Principal",
            "company": "MegaCorp",
            "start_date": "Nov 2023",  # Later start → true most-recent
            "end_date": None,  # Current
            "title_variants": [],
            "achievements": [],
            "skills": [],
        },
    ]

    tailored = {
        "summary": "Experienced analyst",
        "skills": ["Python"],
        "sections": [
            {
                "company": "MegaCorp",
                "title": "Principal",  # The true most-recent role's own title
                "dates": "Nov 2023 - Present",
                "bullets": ["Built systems"],
            }
        ],
        "jd_keywords": ["Python"],
    }

    # Month-aware tiebreak: Principal (Nov 2023) is most-recent, so its own title grounds.
    report = validate_resume_grounding(tailored, profile_concurrent, base_job)
    assert len(report.violations) == 0

    # Discrimination guard: revert the tiebreak to document order (Junior most-recent).
    # 'Principal' is then graded against Junior's allowlist {Junior Analyst, Trainee} and
    # MUST fire — proving this test's outcome depends on the tiebreak.
    monkeypatch.setattr(
        rg, "_identify_most_recent_position", lambda profile: profile.get("positions", [None])[0]
    )
    reverted = validate_resume_grounding(tailored, profile_concurrent, base_job)
    assert any(v.kind in ("title", "title_unlisted") for v in reverted.violations)


# ---------------------------------------------------------------------------
# Test: Prohibited-items derivation (GROUP E)
# ---------------------------------------------------------------------------


def test_prohibited_items_derived_from_style_guide(example_profile, base_job):
    """GROUP E4: Prohibited-items are derived from style_guide, not hardcoded.

    Test (a): a token present in the style guide's banned_tokens fires prohibited_item.
    Test (b): a token NOT in banned_tokens is ALLOWED — override the passed style_guide
    in the test to ban a CUSTOM token and assert it fires, and a style_guide whose
    banned_tokens omits 'dbt' allows 'dbt' (proves derivation, not hardcoding).
    """
    from job_finder.web.resume_tailor import load_style_guide

    # Load the actual style guide
    style_guide = load_style_guide()

    # Test (a): token in banned_tokens fires
    tailored_with_spark = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL", "Apache Spark"],  # Spark is in banned_tokens
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored_with_spark, example_profile, base_job, style_guide)
    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1
    assert any("spark" in v.value.lower() for v in prohibited_violations)

    # Test (b): custom banned token fires
    custom_style_guide = style_guide.copy()
    custom_style_guide["prohibited_items"] = {
        "banned_tokens": ["custom_bad_token"],
        "roi_forbidden_pct": 454,
        "roi_allowed_pct": 350,
        "min_years_anchor": 8,
        "ban_company_name_in_summary": True,
        "ban_sample_sizes": True,
        "ban_em_dash_family": True,
        "ban_third_person": True,
    }

    tailored_with_custom = {
        "summary": "Experienced data scientist with custom_bad_token expertise",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(
        tailored_with_custom, example_profile, base_job, custom_style_guide
    )
    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1
    assert any("custom_bad_token" in v.value.lower() for v in prohibited_violations)

    # Test (c): style_guide without 'dbt' allows 'dbt'
    no_dbt_style_guide = style_guide.copy()
    no_dbt_style_guide["prohibited_items"] = {
        "banned_tokens": ["spark"],  # No dbt
        "roi_forbidden_pct": 454,
        "roi_allowed_pct": 350,
        "min_years_anchor": 8,
        "ban_company_name_in_summary": True,
        "ban_sample_sizes": True,
        "ban_em_dash_family": True,
        "ban_third_person": True,
    }

    tailored_with_dbt = {
        "summary": "Experienced data scientist",
        "skills": ["Python", "SQL"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": ["Built ML pipeline using dbt"],
            }
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(
        tailored_with_dbt, example_profile, base_job, no_dbt_style_guide
    )
    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    # Should NOT catch dbt (it's not in banned_tokens)
    assert not any("dbt" in v.value.lower() for v in prohibited_violations)


def test_example_profile_validates_clean_against_style_guide(example_profile, base_job):
    """GROUP E4: Example profile validates clean against shipped style guide.

    Since 'banned_tokens' includes 'spark', we removed 'Spark' from the example profile.
    The example should trip NO prohibited item.
    """
    from job_finder.web.resume_tailor import load_style_guide

    style_guide = load_style_guide()

    # Build a clean tailored resume from the example profile
    tailored = {
        "summary": "Senior data scientist with expertise in ML pipelines and A/B testing",
        "skills": ["Python", "SQL", "Machine Learning", "A/B Testing"],
        "sections": [
            {
                "company": "TechCorp Solutions",
                "title": "Senior Data Scientist",
                "dates": "Mar 2022 - Present",
                "bullets": [
                    "Built ML pipeline processing 2M+ daily transactions",
                    "Led cross-functional team of 4 to ship recommendation engine",
                ],
            },
            {
                "company": "DataDriven Analytics",
                "title": "Data Scientist",
                "dates": "Jun 2019 - Feb 2022",
                "bullets": [
                    "Developed customer churn prediction model (AUC 0.89)",
                ],
            },
        ],
        "jd_keywords": ["Python", "SQL"],
    }

    report = validate_resume_grounding(tailored, example_profile, base_job, style_guide)

    # Should pass with no violations (including no prohibited items)
    assert len(report.violations) == 0
