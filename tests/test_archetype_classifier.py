"""Tests for deterministic job archetype classification."""

_CONFIG_WITH_ARCHETYPES = {
    "profile": {
        "job_archetypes": {
            "platform_engineering": {
                "keywords": ["platform", "infrastructure", "kubernetes", "devops"],
                "weight_overrides": {"infra_exp": 1.5},
            },
            "ml_engineering": {
                "keywords": ["machine learning", "ml", "model serving", "feature store"],
                "weight_overrides": {},
            },
            "analytics_lead": {
                "keywords": ["analytics", "experimentation", "stakeholder", "roadmap"],
                "weight_overrides": {},
            },
        }
    }
}


class TestClassifyJobArchetype:
    """classify_job_archetype keyword matching."""

    def test_title_match_returns_archetype(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        result = classify_job_archetype(
            "Platform Engineer",
            "Build cloud infrastructure.",
            _CONFIG_WITH_ARCHETYPES,
        )
        assert result == "platform_engineering"

    def test_description_match_returns_archetype(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        result = classify_job_archetype(
            "Senior Engineer",
            "We need someone to build machine learning pipelines.",
            _CONFIG_WITH_ARCHETYPES,
        )
        assert result == "ml_engineering"

    def test_no_match_returns_none(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        result = classify_job_archetype(
            "Receptionist",
            "Greet visitors and answer phones.",
            _CONFIG_WITH_ARCHETYPES,
        )
        assert result is None

    def test_missing_config_section_returns_none(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        assert classify_job_archetype("Platform Engineer", "Build infra.", {}) is None
        assert classify_job_archetype("Platform Engineer", "Build infra.", {"profile": {}}) is None

    def test_case_insensitive_matching(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        result = classify_job_archetype(
            "PLATFORM ENGINEERING LEAD",
            "KUBERNETES DEPLOYMENT",
            _CONFIG_WITH_ARCHETYPES,
        )
        assert result == "platform_engineering"

    def test_first_match_wins(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        # "analytics" matches analytics_lead, "platform" matches platform_engineering
        # but platform_engineering comes first in the dict
        result = classify_job_archetype(
            "Platform Analytics Engineer",
            "Build platform with analytics.",
            _CONFIG_WITH_ARCHETYPES,
        )
        assert result == "platform_engineering"

    def test_word_boundary_matching(self):
        from job_finder.web.archetype_classifier import classify_job_archetype

        # "ml" should match "ml engineer" but not "html"
        result = classify_job_archetype(
            "HTML Developer",
            "Build web pages with HTML.",
            _CONFIG_WITH_ARCHETYPES,
        )
        # "ml" shouldn't match inside "html" due to word boundary
        assert result is None


class TestGetJobArchetypeWeights:
    """get_job_archetype_weights returns weight overrides."""

    def test_returns_overrides_when_present(self):
        from job_finder.web.archetype_classifier import get_job_archetype_weights

        result = get_job_archetype_weights("platform_engineering", _CONFIG_WITH_ARCHETYPES)
        assert result == {"infra_exp": 1.5}

    def test_returns_empty_dict_for_no_overrides(self):
        from job_finder.web.archetype_classifier import get_job_archetype_weights

        result = get_job_archetype_weights("ml_engineering", _CONFIG_WITH_ARCHETYPES)
        assert result == {}

    def test_returns_empty_dict_for_none_archetype(self):
        from job_finder.web.archetype_classifier import get_job_archetype_weights

        assert get_job_archetype_weights(None, _CONFIG_WITH_ARCHETYPES) == {}

    def test_returns_empty_dict_for_unknown_archetype(self):
        from job_finder.web.archetype_classifier import get_job_archetype_weights

        assert get_job_archetype_weights("unknown_type", _CONFIG_WITH_ARCHETYPES) == {}

    def test_returns_empty_dict_for_missing_config(self):
        from job_finder.web.archetype_classifier import get_job_archetype_weights

        assert get_job_archetype_weights("platform_engineering", {}) == {}
