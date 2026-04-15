"""Evaluation framework for benchmarking alternative model providers.

Provides pure functions (Plan 01) and CLI orchestrator (Plan 02):

Pure functions:
- sample_jobs: Query baseline jobs with stored Sonnet scores
- reconstruct_prompt: Rebuild the exact Sonnet prompt from a job row
- compute_metrics: Pearson r, schema adherence rate, latency stats
- compute_verdict: Map metrics to SUITABLE/MARGINAL/NOT_RECOMMENDED
- save_report: Write JSON report to eval_results/ directory

CLI orchestrator (Plan 02):
- run_eval: End-to-end evaluation: sample -> call provider -> metrics -> report
- parse_args: argparse entry point
- main: __main__ entry point

Usage:
    python eval_provider.py --provider gemini --sample-size 20
    python eval_provider.py --provider ollama --model llama3 --yes
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_finder.config import load_config
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import call_model, resolve_provider_config
from job_finder.web.profile_schema import load_profile
from job_finder.web.scoring_types import format_salary_range
from job_finder.web.sonnet_evaluator import SONNET_SCHEMA, _SYSTEM_PROMPT, _BASE_SYSTEM_PROMPT, PROMPT_VARIANTS as _PRODUCTION_VARIANTS


# ---------------------------------------------------------------------------
# Prompt variant system prompts
# ---------------------------------------------------------------------------

PROMPT_VARIANT_DEFAULT = "default"
PROMPT_VARIANT_RUBRIC = "rubric"
PROMPT_VARIANT_FEWSHOT = "fewshot"
PROMPT_VARIANT_FEWSHOT_RUBRIC = "fewshot-rubric"

_RUBRIC_SYSTEM_PROMPT = (
    "You are a senior career advisor evaluating job fit. Analyze the full job description "
    "against the candidate's experience profile.\n\n"
    "## Scoring Rubric\n\n"
    "**90-100 (Exceptional fit):** Candidate meets ALL required qualifications and most "
    "preferred ones. Direct industry experience. Seniority level matches exactly. Location "
    "and salary expectations fully aligned. Should apply immediately.\n\n"
    "**80-89 (Strong fit):** Candidate meets most required qualifications. Relevant "
    "transferable experience compensates for minor gaps. Good seniority alignment. "
    "Location/salary mostly aligned. Worth a strong application.\n\n"
    "**65-79 (Good fit):** Candidate meets core requirements but has notable gaps in "
    "preferred qualifications or industry experience. Seniority within one level. "
    "May require relocation or salary flexibility. Worth applying.\n\n"
    "**50-64 (Partial fit):** Candidate meets some requirements but has significant gaps. "
    "Different industry, missing key technical skills, or seniority mismatch of 2+ levels. "
    "Apply only if very interested in the company/role.\n\n"
    "**30-49 (Weak fit):** Candidate meets few requirements. Major skill gaps, wrong "
    "seniority level, or fundamental mismatch in domain/industry. Not recommended.\n\n"
    "**0-29 (Poor fit):** Fundamental mismatch. Wrong field entirely, entry-level role "
    "for senior candidate (or vice versa), or completely misaligned preferences.\n\n"
    "## Common Scoring Errors to Avoid\n\n"
    "- Do NOT inflate scores for remote roles just because the candidate prefers remote.\n"
    "- Do NOT give 60+ to roles requiring skills the candidate completely lacks.\n"
    "- Do NOT score above 50 when seniority is mismatched by 3+ levels.\n"
    "- DO penalize salary mismatches: if the role pays significantly below minimum, reduce score.\n"
    "- DO distinguish between 'nice to have' and 'required' qualifications.\n\n"
    "Be specific about strengths (cite concrete experience), gaps (be honest but "
    "constructive), and resume priority skills."
)

_FEWSHOT_EXAMPLES = (
    "\n\n## Calibration Examples\n\n"
    "### Example 1: Score 15 (Poor fit)\n"
    "Junior Marketing Coordinator role requiring social media management, content creation, "
    "and 1-2 years marketing experience. Candidate is a Senior Data Scientist with 10+ years "
    "in analytics. Complete domain mismatch, wrong seniority direction.\n\n"
    "### Example 2: Score 38 (Weak fit)\n"
    "Data Engineer role requiring extensive Spark, Kafka, and Airflow experience with AWS "
    "infrastructure. Candidate has strong SQL and Python but minimal distributed systems or "
    "data pipeline engineering experience. Adjacent field but significant skill gaps.\n\n"
    "### Example 3: Score 62 (Partial fit)\n"
    "Product Analytics Manager at a fintech startup requiring team management, A/B testing, "
    "and financial domain knowledge. Candidate has analytics experience and A/B testing but "
    "in healthcare, not finance. No direct reports experience.\n\n"
    "### Example 4: Score 78 (Good fit)\n"
    "Senior Data Scientist at a healthcare company requiring Python, ML, statistical modeling, "
    "and healthcare analytics. Candidate has all technical skills and healthcare domain "
    "experience but is targeting a more senior title (Lead/Staff level).\n\n"
    "### Example 5: Score 91 (Exceptional fit)\n"
    "Staff Data Scientist / Analytics Lead at a health tech SaaS company, remote, $160K-200K. "
    "Requires experimentation design, causal inference, team leadership, Python, SQL. "
    "Candidate matches on every dimension: skills, seniority, domain, location, salary.\n"
)

_FEWSHOT_SYSTEM_PROMPT = _SYSTEM_PROMPT  # _SYSTEM_PROMPT already includes fewshot examples (PRMT-01)

_FEWSHOT_RUBRIC_SYSTEM_PROMPT = _RUBRIC_SYSTEM_PROMPT + _FEWSHOT_EXAMPLES

_ANCHORING_INSTRUCTIONS = (
    "\n\n## Score Anchoring\n\n"
    "CRITICAL: Most jobs should score below 50. A score of 70+ means the candidate is a "
    "near-perfect match on skills, seniority, domain, AND preferences. Do not inflate scores "
    "for remote-friendly roles or well-known companies. A Data Engineer role for a Data "
    "Scientist candidate is a 30-45, not a 65+. An entry-level role for a senior candidate "
    "is 10-20, not 40+. When in doubt, score lower.\n"
)

_COT_INSTRUCTIONS = (
    "\n\n## Evaluation Process\n\n"
    "Before producing your JSON output, reason through these dimensions:\n"
    "1. **Required skills match**: List the top 3 required skills. Does the candidate have "
    "each one? (yes/partial/no)\n"
    "2. **Seniority alignment**: Is the match exact, close (±1 level), or far (±2+ levels)?\n"
    "3. **Domain match**: Is the domain the same, adjacent, or completely different?\n"
    "4. **Preference alignment**: Does the role match target titles, location, salary, industry?\n\n"
    "Use this structured assessment to inform your final score. Include your reasoning in "
    "the summary field.\n"
)

_DISTRIBUTION_INSTRUCTIONS = (
    "\n\n## Expected Score Distribution\n\n"
    "When scoring a diverse batch of jobs, expect approximately:\n"
    "- ~30% should score 0-30 (poor/no fit)\n"
    "- ~30% should score 30-55 (weak/partial fit)\n"
    "- ~25% should score 55-75 (partial/good fit)\n"
    "- ~15% should score 75-100 (good/exceptional fit)\n\n"
    "If your scores cluster above 60 for most jobs, you are inflating. Most jobs in a "
    "general search will NOT be a strong fit for a specific candidate.\n"
)

_COMPARATIVE_ANCHOR = (
    "\n\n## Reference Anchor\n\n"
    "The candidate's IDEAL role would be: Staff Data Scientist / Analytics Lead at a health "
    "tech SaaS company, fully remote, $160K-200K, focused on experimentation design, causal "
    "inference, and team leadership, using Python, SQL, and statistical modeling.\n\n"
    "A score of 100 = identical to this ideal. A score of 50 = shares roughly half the key "
    "attributes. A score of 10 = almost nothing in common. Score each job relative to this "
    "anchor.\n"
)

_STRICT_GATES = (
    "\n\n## Hard Scoring Gates\n\n"
    "Apply these caps BEFORE your final score:\n"
    "- Seniority mismatch > 2 levels (e.g., entry-level for a senior candidate) → cap at 35\n"
    "- Completely different domain (e.g., marketing role for a data scientist) → cap at 25\n"
    "- >50% of required skills are missing → cap at 45\n"
    "- Salary below 70% of candidate minimum → cap at 40\n"
    "- Role title has zero overlap with target titles → reduce by 15 points\n\n"
    "These gates override any other positive signals. A great company with a bad role fit "
    "is still a bad fit.\n"
)

_NEGATIVE_EXAMPLES = (
    "\n\n## Common Scoring Mistakes to Avoid\n\n"
    "**WRONG**: Scoring a Junior Marketing Coordinator role as 55 because "
    "'the candidate could learn marketing.' CORRECT: Score 15 — complete domain mismatch, "
    "wrong seniority direction, no transferable skills.\n\n"
    "**WRONG**: Scoring a Data Engineer role as 65 because 'the candidate knows Python and "
    "SQL.' CORRECT: Score 38 — adjacent field but the candidate lacks Spark, Kafka, Airflow, "
    "and data pipeline engineering experience. Python/SQL overlap alone is not enough.\n\n"
    "**WRONG**: Scoring a VP of Engineering role as 60 because 'the candidate has leadership "
    "potential.' CORRECT: Score 22 — seniority mismatch of 3+ levels, no engineering "
    "management experience, different career track entirely.\n"
)

_FEWSHOT_ANCHORED_PROMPT = _SYSTEM_PROMPT + _ANCHORING_INSTRUCTIONS
_FEWSHOT_COT_PROMPT = _SYSTEM_PROMPT + _COT_INSTRUCTIONS
_FEWSHOT_DISTRIBUTION_PROMPT = _SYSTEM_PROMPT + _DISTRIBUTION_INSTRUCTIONS
_FEWSHOT_COMPARATIVE_PROMPT = _SYSTEM_PROMPT + _COMPARATIVE_ANCHOR
_FEWSHOT_RUBRIC_STRICT_PROMPT = _RUBRIC_SYSTEM_PROMPT + _FEWSHOT_EXAMPLES + _STRICT_GATES
_FEWSHOT_NEGATIVE_PROMPT = _SYSTEM_PROMPT + _NEGATIVE_EXAMPLES

PROMPT_VARIANTS: dict[str, str] = {
    PROMPT_VARIANT_DEFAULT: _BASE_SYSTEM_PROMPT,  # plain prompt without fewshot (legacy eval baseline)
    PROMPT_VARIANT_RUBRIC: _RUBRIC_SYSTEM_PROMPT,
    PROMPT_VARIANT_FEWSHOT: _FEWSHOT_SYSTEM_PROMPT,
    PROMPT_VARIANT_FEWSHOT_RUBRIC: _FEWSHOT_RUBRIC_SYSTEM_PROMPT,
    "fewshot-anchored": _FEWSHOT_ANCHORED_PROMPT,
    "fewshot-cot": _FEWSHOT_COT_PROMPT,
    "fewshot-distribution": _PRODUCTION_VARIANTS["fewshot-distribution"],
    "fewshot-comparative": _FEWSHOT_COMPARATIVE_PROMPT,
    "fewshot-rubric-strict": _FEWSHOT_RUBRIC_STRICT_PROMPT,
    "fewshot-negative": _FEWSHOT_NEGATIVE_PROMPT,
}


# ---------------------------------------------------------------------------
# sample_jobs
# ---------------------------------------------------------------------------


def sample_jobs(conn: Any, n: int, baseline: str = "sonnet") -> list[dict]:
    """Return up to n jobs that have stored baseline scores and full JDs.

    Args:
        conn: Open sqlite3 connection with row_factory=sqlite3.Row.
        n: Maximum number of rows to return.
        baseline: Which score to use as ground truth. "sonnet" (default)
                  filters by sonnet_score IS NOT NULL. "opus" filters by
                  opus_score IS NOT NULL and includes the opus_score column.

    Returns:
        List of plain dicts. Returns empty list when no qualifying rows exist.
    """
    if baseline == "opus":
        rows = conn.execute(
            "SELECT dedup_key, title, company, location, "
            "salary_min, salary_max, jd_full, sonnet_score, "
            "fit_analysis, haiku_score, opus_score "
            "FROM jobs "
            "WHERE opus_score IS NOT NULL AND jd_full IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT dedup_key, title, company, location, "
            "salary_min, salary_max, jd_full, sonnet_score, "
            "fit_analysis, haiku_score "
            "FROM jobs "
            "WHERE sonnet_score IS NOT NULL AND jd_full IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# reconstruct_prompt
# ---------------------------------------------------------------------------


def reconstruct_prompt(
    job_row: dict,
    experience_profile: dict,
    config: dict,
    prompt_variant: str = "default",
) -> tuple[str, str]:
    """Reconstruct the scoring prompt for a given job row.

    Replicates the prompt-building logic from evaluate_job_sonnet() in
    sonnet_evaluator.py. Returns (system_prompt, user_message) where
    system_prompt is selected by prompt_variant.

    Args:
        job_row: Job record dict with keys: title, company, location,
                 salary_min, salary_max, jd_full.
        experience_profile: Experience profile dict with positions, skills,
                            education keys.
        config: Application config dict; reads config["profile"] for
        prompt_variant: System prompt variant to use. One of "default",
                       "rubric", "fewshot", "fewshot-rubric".
                candidate preferences (target_titles, target_locations,
                min_salary, industries).

    Returns:
        Tuple of (system_prompt, user_message) strings matching the format
        sent by evaluate_job_sonnet().
    """
    jd_full = job_row.get("jd_full", "")

    # Build salary string
    salary_min = job_row.get("salary_min")
    salary_max = job_row.get("salary_max")
    salary_str = format_salary_range(salary_min, salary_max)

    # Build experience profile section
    positions = experience_profile.get("positions", [])
    skills = experience_profile.get("skills", [])
    education = experience_profile.get("education", [])

    positions_text = ""
    for pos in positions:
        title = pos.get("title", "")
        company = pos.get("company", "")
        achievements = pos.get("achievements", [])
        pos_skills = pos.get("skills", [])
        achievements_text = (
            "\n".join(f"  - {a}" for a in achievements) if achievements else "  None listed"
        )
        positions_text += (
            f"\n  Role: {title} at {company}\n"
            f"  Skills: {', '.join(pos_skills)}\n"
            f"  Achievements:\n{achievements_text}"
        )

    skills_text = ", ".join(skills) if skills else "Not specified"

    # Candidate Preferences (from config.yaml profile section)
    profile_prefs = config.get("profile", {})
    pref_target_titles = profile_prefs.get("target_titles", [])
    pref_target_locations = profile_prefs.get("target_locations", [])
    pref_min_salary = profile_prefs.get("min_salary")
    pref_industries = profile_prefs.get("industries", [])

    pref_titles_str = ", ".join(pref_target_titles) if pref_target_titles else "Not specified"
    pref_locations_str = (
        ", ".join(pref_target_locations) if pref_target_locations else "Not specified"
    )
    pref_salary_str = f"${pref_min_salary:,}" if pref_min_salary else "Not specified"
    pref_industries_str = ", ".join(pref_industries) if pref_industries else "Not specified"

    user_message = (
        f"## Full Job Description\n\n"
        f"**Title:** {job_row.get('title', 'Unknown Title')}\n"
        f"**Company:** {job_row.get('company', 'Unknown Company')}\n"
        f"**Location:** {job_row.get('location', 'Unknown Location')}\n"
        f"**Salary:** {salary_str}\n\n"
        f"{jd_full}\n\n"
        f"---\n\n"
        f"## Candidate Experience Profile\n\n"
        f"**Key Skills:** {skills_text}\n"
        f"**Positions:**{positions_text}\n\n"
        f"**Education:**\n"
        + (
            "\n".join(
                f"  - {ed.get('degree', '')} - {ed.get('institution', '')} ({ed.get('graduation', '')})"
                + (f" | Thesis: {ed['thesis']}" if ed.get("thesis") else "")
                for ed in education
            )
            if education
            else "  Not specified"
        )
        + "\n\n"
        f"## Candidate Preferences\n\n"
        f"**Target Titles:** {pref_titles_str}\n"
        f"**Target Locations:** {pref_locations_str}\n"
        f"**Minimum Salary:** {pref_salary_str}\n"
        f"**Target Industries:** {pref_industries_str}\n\n"
        f"Evaluate the candidate's fit for this role. Consider both competency match "
        f"(skills, experience) AND preference alignment (title, location, salary, industry). "
        f"Provide structured output."
    )

    system_prompt = PROMPT_VARIANTS.get(prompt_variant, _SYSTEM_PROMPT)
    return (system_prompt, user_message)


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------


def compute_metrics(results: list[dict]) -> dict:
    """Compute aggregate metrics from per-job evaluation results.

    Args:
        results: List of per-job dicts, each with keys:
                 - baseline_score (float | None): stored Sonnet score
                 - eval_score (float | None): score from evaluated provider
                 - schema_valid (bool): whether provider output matched schema
                 - latency_seconds (float): wall-clock time for provider call

    Returns:
        Dict with keys:
        - score_correlation (float | None): Pearson r between baseline and eval
          scores. None when < 2 valid pairs or zero variance in either series.
        - schema_adherence_rate (float): fraction of results where schema_valid=True
        - median_latency_seconds (float): median of latency_seconds values
        - mean_latency_seconds (float): mean of latency_seconds values
        - mean_delta (float | None): avg(eval - baseline) — raw bias. Positive = inflated.
        - mean_absolute_error (float | None): avg(|eval - baseline|) — accuracy.
        - score_std_delta (float | None): stdev of deltas — consistency.
        - bucket_deltas (dict): avg delta per baseline bucket {low, mid, high}.
        - baseline_distribution (dict): job count per bucket {low, mid, high}.
    """
    # Filter to pairs where both baseline and eval scores exist
    valid_pairs = [
        (r["baseline_score"], r["eval_score"])
        for r in results
        if r.get("baseline_score") is not None and r.get("eval_score") is not None
    ]

    score_correlation: float | None = None
    if len(valid_pairs) >= 2:
        baseline_scores, eval_scores = zip(*valid_pairs)
        try:
            score_correlation = statistics.correlation(baseline_scores, eval_scores)
        except statistics.StatisticsError:
            # Zero variance: all values identical — Pearson r undefined
            score_correlation = None

    schema_adherence_rate = sum(1 for r in results if r.get("schema_valid")) / len(results)

    latencies = [r["latency_seconds"] for r in results]
    median_latency = statistics.median(latencies)
    mean_latency = statistics.mean(latencies)

    # --- Bias / accuracy metrics ---
    mean_delta: float | None = None
    mean_absolute_error: float | None = None
    score_std_delta: float | None = None
    bucket_deltas: dict[str, float | None] = {"low": None, "mid": None, "high": None}
    baseline_distribution: dict[str, int] = {"low": 0, "mid": 0, "high": 0}

    if len(valid_pairs) >= 2:
        deltas = [eval_s - base_s for base_s, eval_s in valid_pairs]
        mean_delta = statistics.fmean(deltas)
        mean_absolute_error = statistics.fmean([abs(d) for d in deltas])
        score_std_delta = statistics.stdev(deltas) if len(deltas) >= 2 else 0.0

        # Bucket analysis: low (<30), mid (30-60), high (60+)
        buckets: dict[str, list[float]] = {"low": [], "mid": [], "high": []}
        for base_s, eval_s in valid_pairs:
            delta = eval_s - base_s
            if base_s < 30:
                buckets["low"].append(delta)
            elif base_s < 60:
                buckets["mid"].append(delta)
            else:
                buckets["high"].append(delta)

        for bucket_name, bucket_list in buckets.items():
            baseline_distribution[bucket_name] = len(bucket_list)
            if bucket_list:
                bucket_deltas[bucket_name] = statistics.fmean(bucket_list)

    return {
        "score_correlation": score_correlation,
        "schema_adherence_rate": schema_adherence_rate,
        "median_latency_seconds": median_latency,
        "mean_latency_seconds": mean_latency,
        "mean_delta": mean_delta,
        "mean_absolute_error": mean_absolute_error,
        "score_std_delta": score_std_delta,
        "bucket_deltas": bucket_deltas,
        "baseline_distribution": baseline_distribution,
    }


# ---------------------------------------------------------------------------
# compute_verdict
# ---------------------------------------------------------------------------


def compute_verdict(
    pearson_r: float | None,
    adherence_rate: float,
    thresholds: dict,
    *,
    mean_absolute_error: float | None = None,
    mean_delta: float | None = None,
) -> str:
    """Map metrics to a SUITABLE/MARGINAL/NOT_RECOMMENDED verdict.

    Verdict logic (strict AND — all dimensions must pass):
    - SUITABLE: ALL metrics meet SUITABLE thresholds
    - MARGINAL: all meet MARGINAL thresholds but at least one misses SUITABLE
    - NOT_RECOMMENDED: any metric misses MARGINAL threshold

    Dimensions: correlation, schema adherence, MAE (accuracy), bias (mean delta).
    Latency is informational only and does not affect the verdict.

    Args:
        pearson_r: Pearson r correlation coefficient, or None (insufficient data).
        adherence_rate: Fraction of results with schema_valid=True (0.0-1.0).
        thresholds: Dict with threshold keys. Missing keys use defaults:
            correlation_suitable (0.85), correlation_marginal (0.70),
            adherence_suitable (0.95), adherence_marginal (0.80),
            mae_suitable (15.0), mae_marginal (25.0),
            bias_suitable (10.0), bias_marginal (20.0).
        mean_absolute_error: Average |eval - baseline|. None skips the check
            (backward compat for old callers).
        mean_delta: Average (eval - baseline) bias. None skips the check.

    Returns:
        "SUITABLE", "MARGINAL", or "NOT_RECOMMENDED".
    """
    corr_suitable = thresholds.get("correlation_suitable", 0.85)
    corr_marginal = thresholds.get("correlation_marginal", 0.70)
    adh_suitable = thresholds.get("adherence_suitable", 0.95)
    adh_marginal = thresholds.get("adherence_marginal", 0.80)
    mae_suitable_t = thresholds.get("mae_suitable", 15.0)
    mae_marginal_t = thresholds.get("mae_marginal", 25.0)
    bias_suitable_t = thresholds.get("bias_suitable", 10.0)
    bias_marginal_t = thresholds.get("bias_marginal", 20.0)

    if pearson_r is None:
        corr_ok_suitable = False
        corr_ok_marginal = False
    else:
        corr_ok_suitable = pearson_r >= corr_suitable
        corr_ok_marginal = pearson_r >= corr_marginal

    adh_ok_suitable = adherence_rate >= adh_suitable
    adh_ok_marginal = adherence_rate >= adh_marginal

    # MAE and bias: skip check when None (backward compat / insufficient data)
    if mean_absolute_error is not None:
        mae_ok_suitable = mean_absolute_error <= mae_suitable_t
        mae_ok_marginal = mean_absolute_error <= mae_marginal_t
    else:
        mae_ok_suitable = True
        mae_ok_marginal = True

    if mean_delta is not None:
        bias_ok_suitable = abs(mean_delta) <= bias_suitable_t
        bias_ok_marginal = abs(mean_delta) <= bias_marginal_t
    else:
        bias_ok_suitable = True
        bias_ok_marginal = True

    all_suitable = corr_ok_suitable and adh_ok_suitable and mae_ok_suitable and bias_ok_suitable
    all_marginal = corr_ok_marginal and adh_ok_marginal and mae_ok_marginal and bias_ok_marginal

    if all_suitable:
        return "SUITABLE"
    elif all_marginal:
        return "MARGINAL"
    else:
        return "NOT_RECOMMENDED"


# ---------------------------------------------------------------------------
# save_report
# ---------------------------------------------------------------------------


def save_report(
    report: dict,
    provider: str,
    output_dir: str = "eval_results",
) -> Path:
    """Write an evaluation report to a JSON file in output_dir.

    Args:
        report: Report dict; should contain "meta", "aggregate", "per_job" keys.
        provider: Provider name used in the filename (e.g. "gemini", "ollama").
        output_dir: Directory to write the report (default: "eval_results").
                    Created if it does not exist.

    Returns:
        Path to the written JSON file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{provider}_{timestamp}.json"
    file_path = output_path / filename

    file_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# run_eval orchestrator
# ---------------------------------------------------------------------------


def run_eval(
    provider: str,
    model: str | None,
    sample_size: int,
    thresholds: dict,
    skip_confirm: bool = False,
    delay: float = 0.0,
    retries: int = 0,
    prompt_variant: str = "default",
    baseline: str = "sonnet",
) -> None:
    """Orchestrate a full provider evaluation: sample, call, measure, report.

    Args:
        provider: Provider name to evaluate.
        model: Model identifier override. If None, use config's default for sonnet tier.
        sample_size: Number of jobs to sample from the DB.
        thresholds: Dict with keys correlation_suitable, correlation_marginal,
                    adherence_suitable, adherence_marginal.
        skip_confirm: If True, skip the confirmation prompt.
        delay: Seconds to wait between API calls (for rate-limited providers).
        retries: Number of retries on 429/5xx errors with exponential backoff.
        prompt_variant: System prompt variant ("default", "rubric", "fewshot",
                       "fewshot-rubric").
        baseline: Ground-truth score to compare against ("sonnet" or "opus").
    """
    # 1. Load config and profile
    config = load_config()
    experience_profile = load_profile()

    # 2. Build eval config override — route sonnet tier to target provider, no fallback
    resolved = resolve_provider_config("sonnet", config)
    eval_model = model or resolved["model"]
    eval_config = dict(config)
    eval_config["providers"] = {
        "sonnet": {
            "provider": provider,
            "model": eval_model,
            "fallback": None,
        }
    }

    # 3. Open DB and sample jobs — connection wraps entire loop (call_model needs it)
    with standalone_connection(config["db"]["path"]) as conn:
        jobs = sample_jobs(conn, sample_size, baseline=baseline)

        if len(jobs) == 0:
            baseline_label = "Opus" if baseline == "opus" else "Sonnet"
            print(f"No {baseline_label}-scored jobs found. Run baseline scoring first.")
            sys.exit(1)

        # 4. Confirmation prompt
        if not skip_confirm:
            print(f"\nEvaluation Plan:")
            print(f"  Provider : {provider}")
            print(f"  Model    : {eval_model}")
            print(f"  Jobs     : {len(jobs)}")
            print(f"  Prompt   : {prompt_variant}")
            print(f"  Baseline : {baseline}")
            if delay > 0:
                print(f"  Delay    : {delay:.1f}s between calls")
            if retries > 0:
                print(f"  Retries  : {retries} (exponential backoff)")
            answer = input("\nProceed? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)

        # 5. Per-job evaluation loop
        per_job_results: list[dict] = []
        total = len(jobs)

        for i, job in enumerate(jobs):
            system, user_message = reconstruct_prompt(
                job, experience_profile, config, prompt_variant=prompt_variant
            )
            messages = [{"role": "user", "content": user_message}]

            # Throttle: delay between calls (skip before first call)
            if delay > 0 and i > 0:
                time.sleep(delay)

            t0 = time.perf_counter()
            eval_score: float | None = None
            schema_valid = False
            error: str | None = None

            for attempt in range(1 + retries):
                try:
                    result = call_model(
                        tier="sonnet",
                        system=system,
                        messages=messages,
                        conn=conn,
                        config=eval_config,
                        output_schema=SONNET_SCHEMA,
                        job_id=job["dedup_key"],
                        purpose="provider_eval",
                        max_tokens=2048,
                        client=None,
                    )
                    eval_score = result.data.get("score")
                    schema_valid = result.schema_valid
                    error = None
                    break
                except Exception as exc:
                    error = str(exc)
                    is_retryable = "429" in error or "500" in error or "502" in error or "503" in error
                    if is_retryable and attempt < retries:
                        backoff = min(2 ** attempt * 5, 120)
                        print(f"  -> Retry {attempt + 1}/{retries} in {backoff}s...")
                        time.sleep(backoff)
                    else:
                        break

            latency = time.perf_counter() - t0

            baseline_score = job["opus_score"] if baseline == "opus" else job["sonnet_score"]
            print(
                f"[{i + 1}/{total}] {job['title']} @ {job['company']} "
                f"- score: {eval_score} (baseline: {baseline_score}) {latency:.1f}s"
                + (f" [ERROR: {error}]" if error else "")
            )

            per_job_results.append({
                "dedup_key": job["dedup_key"],
                "title": job["title"],
                "company": job["company"],
                "baseline_score": baseline_score,
                "eval_score": eval_score,
                "schema_valid": schema_valid,
                "latency_seconds": latency,
                "error": error,
            })

        # 6. Compute metrics and verdict
        metrics = compute_metrics(per_job_results)
        verdict = compute_verdict(
            metrics.get("score_correlation"),
            metrics["schema_adherence_rate"],
            thresholds,
            mean_absolute_error=metrics.get("mean_absolute_error"),
            mean_delta=metrics.get("mean_delta"),
        )

        # 7. Build and save report
        report = {
            "meta": {
                "provider": provider,
                "model": eval_model,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sample_size": len(jobs),
                "db_path": config["db"]["path"],
                "prompt_variant": prompt_variant,
                "baseline": baseline,
                "delay_seconds": delay,
                "retries": retries,
            },
            "aggregate": {
                **metrics,
                "verdict": verdict,
                "thresholds_used": thresholds,
            },
            "per_job": per_job_results,
        }
        report_path = save_report(report, provider)

    # 8. Print summary
    _supports_color = sys.stdout.isatty()
    _verdict_colors = {
        "SUITABLE": "\033[92m",      # green
        "MARGINAL": "\033[93m",      # yellow
        "NOT_RECOMMENDED": "\033[91m",  # red
    }
    _reset = "\033[0m"

    verdict_display = verdict
    if _supports_color and verdict in _verdict_colors:
        verdict_display = f"{_verdict_colors[verdict]}{verdict}{_reset}"

    corr = metrics.get("score_correlation")
    corr_str = f"{corr:.3f}" if corr is not None else "N/A (insufficient data)"

    delta = metrics.get("mean_delta")
    delta_str = f"{delta:+.1f}" if delta is not None else "N/A"
    mae = metrics.get("mean_absolute_error")
    mae_str = f"{mae:.1f}" if mae is not None else "N/A"
    std_d = metrics.get("score_std_delta")
    std_str = f"{std_d:.1f}" if std_d is not None else "N/A"

    print("\n" + "=" * 60)
    print(f"VERDICT: {verdict_display}")
    print("=" * 60)
    print(f"  Score correlation (Pearson r) : {corr_str}")
    print(f"  Mean delta (bias)             : {delta_str}")
    print(f"  Mean absolute error           : {mae_str}")
    print(f"  Score delta std dev           : {std_str}")
    print(f"  Schema adherence rate         : {metrics['schema_adherence_rate']:.1%}")
    print(f"  Median latency                : {metrics['median_latency_seconds']:.1f}s")
    print(f"  Mean latency                  : {metrics['mean_latency_seconds']:.1f}s")

    bucket_deltas = metrics.get("bucket_deltas", {})
    baseline_dist = metrics.get("baseline_distribution", {})
    if any(v is not None for v in bucket_deltas.values()):
        parts = []
        for b in ("low", "mid", "high"):
            d = bucket_deltas.get(b)
            n = baseline_dist.get(b, 0)
            parts.append(f"{b}={d:+.1f}(n={n})" if d is not None else f"{b}=-(n={n})")
        print(f"  Bucket deltas (<30/30-60/60+) : {' | '.join(parts)}")

    print(f"  Report saved to               : {report_path}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# argparse CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for eval_provider.

    Args:
        argv: Argument list (default: sys.argv[1:]).

    Returns:
        Parsed argparse.Namespace.
    """
    parser = argparse.ArgumentParser(
        prog="eval_provider",
        description=(
            "Benchmark an alternative model provider against stored Sonnet scores.\n\n"
            "Samples jobs with baseline Sonnet scores from the DB, calls the target\n"
            "provider with the same prompts, and computes score correlation and schema\n"
            "adherence to produce a SUITABLE/MARGINAL/NOT_RECOMMENDED verdict."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=["cohere", "gemini", "mistral", "ollama", "ollm", "sambanova"],
        help="Provider to evaluate.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model identifier override (e.g. 'gemini-1.5-pro', 'llama3'). "
            "Defaults to the model configured for the sonnet tier."
        ),
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=20,
        metavar="N",
        help="Number of jobs to sample from the DB (default: 20).",
    )
    parser.add_argument(
        "--correlation-suitable",
        type=float,
        default=0.85,
        metavar="R",
        help="Pearson r threshold for SUITABLE verdict (default: 0.85).",
    )
    parser.add_argument(
        "--correlation-marginal",
        type=float,
        default=0.70,
        metavar="R",
        help="Pearson r threshold for MARGINAL verdict (default: 0.70).",
    )
    parser.add_argument(
        "--adherence-suitable",
        type=float,
        default=0.95,
        metavar="RATE",
        help="Schema adherence rate threshold for SUITABLE verdict (default: 0.95).",
    )
    parser.add_argument(
        "--adherence-marginal",
        type=float,
        default=0.80,
        metavar="RATE",
        help="Schema adherence rate threshold for MARGINAL verdict (default: 0.80).",
    )
    parser.add_argument(
        "--mae-suitable",
        type=float,
        default=15.0,
        metavar="MAE",
        help="Mean absolute error threshold for SUITABLE verdict (default: 15.0).",
    )
    parser.add_argument(
        "--mae-marginal",
        type=float,
        default=25.0,
        metavar="MAE",
        help="Mean absolute error threshold for MARGINAL verdict (default: 25.0).",
    )
    parser.add_argument(
        "--bias-suitable",
        type=float,
        default=10.0,
        metavar="BIAS",
        help="Absolute mean delta (bias) threshold for SUITABLE verdict (default: 10.0).",
    )
    parser.add_argument(
        "--bias-marginal",
        type=float,
        default=20.0,
        metavar="BIAS",
        help="Absolute mean delta (bias) threshold for MARGINAL verdict (default: 20.0).",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt (non-interactive mode).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SECS",
        help="Seconds to wait between API calls (default: 0). Useful for rate-limited providers.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        metavar="N",
        help="Number of retries on 429/5xx errors with exponential backoff (default: 0).",
    )
    parser.add_argument(
        "--prompt-variant",
        default="default",
        choices=list(PROMPT_VARIANTS.keys()),
        help="System prompt variant for scoring (default: default).",
    )
    parser.add_argument(
        "--baseline",
        default="sonnet",
        choices=["sonnet", "opus"],
        help="Baseline score to compare against (default: sonnet).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI args and run provider evaluation."""
    args = parse_args()
    thresholds = {
        "correlation_suitable": args.correlation_suitable,
        "correlation_marginal": args.correlation_marginal,
        "adherence_suitable": args.adherence_suitable,
        "adherence_marginal": args.adherence_marginal,
        "mae_suitable": args.mae_suitable,
        "mae_marginal": args.mae_marginal,
        "bias_suitable": args.bias_suitable,
        "bias_marginal": args.bias_marginal,
    }
    run_eval(
        provider=args.provider,
        model=args.model,
        sample_size=args.sample_size,
        thresholds=thresholds,
        skip_confirm=args.yes,
        delay=args.delay,
        retries=args.retries,
        prompt_variant=args.prompt_variant,
        baseline=args.baseline,
    )


if __name__ == "__main__":
    main()
