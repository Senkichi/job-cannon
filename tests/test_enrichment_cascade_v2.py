"""Tests for the synthesis-free enrichment cascade.

After Phase 2b sub-fix (RC4), the cascade no longer contains the LLM
synthesis tiers (haiku/sonnet) which fabricated short pseudo-JDs from
fragments and blocked escalation to true fetch tiers. The new cascade is
strictly fetch-based: free -> ddg -> serpapi -> agentic -> exhausted.
"""

from job_finder.web.data_enricher import FIELD_TIER_CEILINGS, TIER_ORDER


def test_tier_order_excludes_haiku_and_sonnet():
    assert "haiku" not in TIER_ORDER
    assert "sonnet" not in TIER_ORDER
    assert TIER_ORDER == ["free", "ddg", "serpapi", "agentic", "exhausted"]


def test_field_tier_ceiling_for_jd_full_caps_at_agentic():
    assert FIELD_TIER_CEILINGS["jd_full"] == "agentic"


def test_field_tier_ceilings_no_haiku_or_sonnet_references():
    for field, ceiling in FIELD_TIER_CEILINGS.items():
        assert ceiling not in ("haiku", "sonnet"), (
            f"Field {field} still references deleted tier {ceiling}"
        )
