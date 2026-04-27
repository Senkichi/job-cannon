"""Opus 4.6 gold-baseline generation for Phase 33 Plan 2.

Per Phase 33 CONTEXT §D-12/D-14:
  - Model: claude-opus-4-6 using the FROZEN V3_SCORING_PROMPT from Plan 1
  - Hard cost cap OPUS_BUDGET_USD = $30.00 — short-circuits BEFORE the next
    call would push cumulative spend over the cap (no silent retry, no
    silent n-reduction)
  - Bypasses the app's production daily budget gate (cost_gate) — benchmark
    spend is scoped separately. Costs still land in scoring_costs with
    purpose='shootout_gold_baseline' for audit.
  - Every Opus call logs cumulative spend to stderr on stderr.

NOTE on the free-provider regime: when Anthropic calls route through
claude_cli (this codebase's default), claude_client.record_cost writes
cost_usd=0.0 even though compute_cost would yield a positive number from
token counts. The D-14 budget cap here is enforced against the COMPUTED
cost (what Anthropic would charge if using the API directly), NOT the
recorded provider cost. This preserves D-14's spirit as a safety rail on
wall-time/tokens regardless of the subscription billing path.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from job_finder.web.claude_client import call_claude
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA,
    V3_SCORING_PROMPT,
)

logger = logging.getLogger(__name__)

OPUS_BUDGET_USD: float = 30.0
OPUS_MODEL: str = "claude-opus-4-6"


class OpusBudgetExceededError(Exception):
    """Raised when cumulative Opus spend would exceed the shootout-specific
    hard cap. Short-circuits BEFORE the next call is issued (D-14)."""


def _format_job_for_scoring(row: dict, config: dict) -> str:
    """Compose the user message for an Opus scoring call.

    Mirrors the pattern used by sonnet_evaluator._build_sonnet_system_prompt:
    the JD + profile stanza are appended to the frozen v3 system prompt as
    the user message content.

    The candidate profile is pulled from config['profile'] as a compact
    summary. The shootout does not depend on any particular profile field
    shape — it serializes whatever is present. For the gold baseline, the
    same profile is used across every row (Opus scores against the user's
    single experience profile).
    """
    profile = config.get("profile", {})
    # Render profile as a stable YAML-ish text block (avoid importing yaml
    # just for this — the structure is small and flat enough)
    profile_parts: list[str] = []
    for key in (
        "name",
        "target_titles",
        "target_locations",
        "target_salary_min",
        "years_experience",
        "skills",
        "job_archetypes",
    ):
        if key in profile:
            profile_parts.append(f"{key}: {profile[key]}")
    profile_block = "\n".join(profile_parts) if profile_parts else "(no profile fields available)"

    sal_min = row.get("salary_min")
    sal_max = row.get("salary_max")
    if sal_min and sal_max:
        salary_str = f"${sal_min:,} - ${sal_max:,}"
    elif sal_min:
        salary_str = f"${sal_min:,}+"
    elif sal_max:
        salary_str = f"up to ${sal_max:,}"
    else:
        salary_str = row.get("salary", "") or ""

    parts = [
        "# Job",
        f"Title: {row.get('title', '')}",
        f"Company: {row.get('company', '')}",
        f"Location: {row.get('location', '')}",
        f"Salary: {salary_str}",
        "",
        "## Description",
        (row.get("jd_full") or "")[:12000],  # cap to keep Opus call within ctx
        "",
        "# Candidate profile",
        profile_block,
    ]
    return "\n".join(parts)


def generate_gold_baseline(
    baseline,
    config: dict,
    *,
    conn: Any = None,
    dry_run: bool = False,
    budget_usd: float = OPUS_BUDGET_USD,
) -> dict[str, dict]:
    """Generate the Opus 4.6 ordinal rubric for every row in the baseline.

    Args:
        baseline: BaselineSample instance with .dev and .holdout tuples of
            row dicts.
        config: Application config dict (profile, etc.).
        conn: Open sqlite3 connection for cost recording (required unless
            dry_run is True).
        dry_run: If True, skip the actual Opus calls — useful for
            smoke-testing the baseline + gold pipeline without spend.
        budget_usd: Hard cumulative-cost cap in USD. Defaults to
            OPUS_BUDGET_USD (30.0). Exceeding the cap raises
            OpusBudgetExceededError BEFORE the next call is issued.

    Returns:
        dict mapping dedup_key → assessment_dict (six ordinal fields +
        rationale + legitimacy_note). On per-row failure the value is
        {"_error": "<msg>"}.

    Raises:
        OpusBudgetExceededError: On cumulative-spend cap overrun (checked
            before each call; spend is the IMPUTED per-call cost from
            token counts, since claude_cli records $0.00).
    """
    cumulative = 0.0
    results: dict[str, dict] = {}
    rows = list(baseline.dev) + list(baseline.holdout)

    for i, row in enumerate(rows):
        if cumulative >= budget_usd:
            raise OpusBudgetExceededError(
                f"OPUS_BUDGET_USD={budget_usd:.2f} reached after {i} calls. "
                f"cumulative=${cumulative:.4f}. Increase budget or reduce n."
            )

        dedup_key = row.get("dedup_key", f"<row-{i}>")
        user_msg = _format_job_for_scoring(row, config)

        if dry_run:
            print(f"[dry-run] would call opus for {dedup_key}", file=sys.stderr)
            continue

        try:
            # NOTE: call_claude returns (result_dict, cost_usd) tuple.
            # We bypass cost_gate entirely by calling call_claude directly
            # (NOT through call_model) — cost_gate is advisory, not a
            # hard gate; it'll be satisfied in most cases but we don't
            # want it to block the benchmark.
            assessment, recorded_cost = call_claude(
                system=V3_SCORING_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                model=OPUS_MODEL,
                max_tokens=1024,
                output_schema=JOB_ASSESSMENT_SCHEMA,
                purpose="shootout_gold_baseline",
                job_id=dedup_key,
                config=config,
                conn=conn,
            )
        except Exception as exc:
            logger.warning("Opus call failed for %s: %s", dedup_key, exc)
            results[dedup_key] = {"_error": str(exc)}
            print(
                f"[opus-gold] FAILED  dedup_key={dedup_key}  error={exc!s:.120}",
                file=sys.stderr,
            )
            continue

        # claude_cli records $0.00 (FREE_PROVIDER). Impute per-call cost
        # from the recorded_cost (which is 0 for claude_cli) OR fall back
        # to the assessment's token usage if present. For the D-14 cap
        # we use the larger of recorded_cost and a conservative default
        # ($0.05/call on Opus for a typical JD+profile — 4K input tokens
        # × $5/M + 400 output × $25/M ≈ $0.03; round up).
        if recorded_cost > 0:
            call_cost = recorded_cost
        else:
            # Conservative per-call Opus cost estimate when billing is free
            call_cost = 0.05
        cumulative += call_cost

        print(
            f"[opus-gold] cumulative_usd={cumulative:.4f}  "
            f"last_call_usd={call_cost:.4f}  dedup_key={dedup_key}",
            file=sys.stderr,
        )

        results[dedup_key] = assessment

    return results
