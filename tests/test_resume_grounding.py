"""Tests for resume grounding validator (issue #600)."""

from pathlib import Path

import pytest

from job_finder.web.profile_schema import load_profile
from job_finder.web.resume_grounding import validate_resume_grounding

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


# ---------------------------------------------------------------------------
# Test: Layer A - structural subset (fabricated facts)
# ---------------------------------------------------------------------------


def test_validator_catches_fabricated_employer(example_profile, base_job):
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

    report = validate_resume_grounding(tailored, example_profile, base_job)

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


# ---------------------------------------------------------------------------
# Test: Layer B - prohibited items
# ---------------------------------------------------------------------------


def test_validator_catches_prohibited_items(example_profile, base_job):
    """Layer B: All mechanized hard-stops are caught."""
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

    report = validate_resume_grounding(tailored, example_profile, base_job)

    # Should catch multiple prohibited items
    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 4  # At least dbt, 454%, N=, em-dash

    # Check specific items
    violation_values = [v.value for v in prohibited_violations]
    assert any("dbt" in val.lower() for val in violation_values)
    assert any("454" in val for val in violation_values)
    assert any("N=" in val for val in violation_values)
    assert any("—" in val for val in violation_values)


def test_validator_catches_spark_prohibited(example_profile, base_job):
    """Layer B: Apache Spark is caught."""
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

    report = validate_resume_grounding(tailored, example_profile, base_job)

    prohibited_violations = [v for v in report.violations if v.kind == "prohibited_item"]
    assert len(prohibited_violations) >= 1
    assert any("spark" in v.value.lower() for v in prohibited_violations)


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

    config = {}
    conn = Mock()

    with patch("job_finder.web.model_provider.call_model", return_value=mock_result):
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
        result = tailor_resume(base_job, example_profile, config, conn)

        # Should return normally with coverage metric
        assert "keyword_coverage" in result
        assert result["keyword_coverage"]["ratio"] == 1.0
