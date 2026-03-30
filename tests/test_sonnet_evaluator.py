"""Tests for sonnet_evaluator prompt configuration (PRMT-01, PRMT-02)."""


def test_system_prompt_includes_fewshot():
    """_SYSTEM_PROMPT includes fewshot calibration examples by default (PRMT-01)."""
    from job_finder.web.sonnet_evaluator import _SYSTEM_PROMPT
    assert "Calibration Examples" in _SYSTEM_PROMPT
    assert "Score 15 (Poor fit)" in _SYSTEM_PROMPT
    assert "Score 91 (Exceptional fit)" in _SYSTEM_PROMPT


def test_prompt_variants_dict_exists():
    """PROMPT_VARIANTS dict exists with required keys (PRMT-02)."""
    from job_finder.web.sonnet_evaluator import PROMPT_VARIANTS
    assert "fewshot" in PROMPT_VARIANTS
    assert "fewshot-distribution" in PROMPT_VARIANTS


def test_fewshot_distribution_includes_distribution_instructions():
    """fewshot-distribution variant includes score distribution awareness (PRMT-02)."""
    from job_finder.web.sonnet_evaluator import PROMPT_VARIANTS
    dist_prompt = PROMPT_VARIANTS["fewshot-distribution"]
    assert "Expected Score Distribution" in dist_prompt
    assert "~30% should score 0-30" in dist_prompt
    # Also includes the fewshot examples (superset of default)
    assert "Calibration Examples" in dist_prompt


def test_base_system_prompt_has_no_fewshot():
    """_BASE_SYSTEM_PROMPT is the plain prompt without fewshot (for eval_provider default variant)."""
    from job_finder.web.sonnet_evaluator import _BASE_SYSTEM_PROMPT
    assert "Calibration Examples" not in _BASE_SYSTEM_PROMPT
    assert "career advisor evaluating job fit" in _BASE_SYSTEM_PROMPT


def test_eval_provider_fewshot_variant_not_doubled():
    """eval_provider PROMPT_VARIANTS['fewshot'] does not double-include fewshot examples."""
    from eval_provider import PROMPT_VARIANTS
    fewshot_prompt = PROMPT_VARIANTS["fewshot"]
    # Count occurrences of a unique fewshot marker
    count = fewshot_prompt.count("Score 15 (Poor fit)")
    assert count == 1, f"Fewshot examples appear {count} times (expected 1)"
