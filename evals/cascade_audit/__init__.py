"""Cascade audit eval harness (Phase 36).

Provides shadow-replay evaluation infrastructure for comparing LLM outputs
across providers using a DeepSeek-V4-Flash judge for the 6 non-scoring callsites:
parse_structured_fields, find_careers_url, extract_jobs, description_reformat,
company_research, ai_nav_discovery.
"""

from evals.cascade_audit.corpus_loader import CorpusLoader
from evals.cascade_audit.judge import JUDGE_SYSTEM_PROMPT, judge_pair, judge_with_position_swap
from evals.cascade_audit.verdict import Verdict

__all__ = [
    "JUDGE_SYSTEM_PROMPT",
    "CorpusLoader",
    "Verdict",
    "judge_pair",
    "judge_with_position_swap",
]
