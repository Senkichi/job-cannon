"""Evaluation framework pure functions for benchmarking alternative model providers.

Provides five pure functions used by the CLI evaluation tool (Plan 02):
- sample_jobs: Query baseline jobs with stored Sonnet scores
- reconstruct_prompt: Rebuild the exact Sonnet prompt from a job row
- compute_metrics: Pearson r, schema adherence rate, latency stats
- compute_verdict: Map metrics to SUITABLE/MARGINAL/NOT_RECOMMENDED
- save_report: Write JSON report to eval_results/ directory

All functions are side-effect-free except save_report (writes a file).
No Flask dependencies — designed for standalone CLI use.

Usage (via Plan 02 CLI wrapper):
    python eval_provider.py --provider gemini --sample-size 20
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from job_finder.web.scoring_types import format_salary_range
from job_finder.web.sonnet_evaluator import _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# sample_jobs
# ---------------------------------------------------------------------------


def sample_jobs(conn: Any, n: int) -> list[dict]:
    """Return up to n jobs that have stored Sonnet scores and full job descriptions.

    Args:
        conn: Open sqlite3 connection with row_factory=sqlite3.Row.
        n: Maximum number of rows to return.

    Returns:
        List of plain dicts with keys: dedup_key, title, company, location,
        salary_min, salary_max, jd_full, sonnet_score, fit_analysis, haiku_score.
        Returns empty list when no qualifying rows exist.
    """
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
) -> tuple[str, str]:
    """Reconstruct the exact Sonnet prompt for a given job row.

    Replicates the prompt-building logic from evaluate_job_sonnet() in
    sonnet_evaluator.py. Returns (system_prompt, user_message) where
    system_prompt is imported directly from sonnet_evaluator to ensure
    no drift.

    Args:
        job_row: Job record dict with keys: title, company, location,
                 salary_min, salary_max, jd_full.
        experience_profile: Experience profile dict with positions, skills,
                            education keys.
        config: Application config dict; reads config["profile"] for
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
                f"  - {ed.get('degree', '')} — {ed.get('institution', '')} ({ed.get('graduation', '')})"
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

    return (_SYSTEM_PROMPT, user_message)


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

    return {
        "score_correlation": score_correlation,
        "schema_adherence_rate": schema_adherence_rate,
        "median_latency_seconds": median_latency,
        "mean_latency_seconds": mean_latency,
    }


# ---------------------------------------------------------------------------
# compute_verdict
# ---------------------------------------------------------------------------


def compute_verdict(
    pearson_r: float | None,
    adherence_rate: float,
    thresholds: dict,
) -> str:
    """Map metrics to a SUITABLE/MARGINAL/NOT_RECOMMENDED verdict.

    Verdict logic (strict):
    - SUITABLE: ALL metrics meet SUITABLE thresholds
    - MARGINAL: all meet MARGINAL thresholds but at least one misses SUITABLE
    - NOT_RECOMMENDED: any metric misses MARGINAL threshold

    Latency is informational only and does not affect the verdict.

    Args:
        pearson_r: Pearson r correlation coefficient, or None (insufficient data).
        adherence_rate: Fraction of results with schema_valid=True (0.0-1.0).
        thresholds: Dict with keys correlation_suitable, correlation_marginal,
                    adherence_suitable, adherence_marginal. Missing keys use
                    defaults (0.85, 0.70, 0.95, 0.80).

    Returns:
        "SUITABLE", "MARGINAL", or "NOT_RECOMMENDED".
    """
    corr_suitable = thresholds.get("correlation_suitable", 0.85)
    corr_marginal = thresholds.get("correlation_marginal", 0.70)
    adh_suitable = thresholds.get("adherence_suitable", 0.95)
    adh_marginal = thresholds.get("adherence_marginal", 0.80)

    if pearson_r is None:
        # Cannot compute correlation — insufficient data or zero variance
        corr_ok_suitable = False
        corr_ok_marginal = False
    else:
        corr_ok_suitable = pearson_r >= corr_suitable
        corr_ok_marginal = pearson_r >= corr_marginal

    adh_ok_suitable = adherence_rate >= adh_suitable
    adh_ok_marginal = adherence_rate >= adh_marginal

    if corr_ok_suitable and adh_ok_suitable:
        return "SUITABLE"
    elif corr_ok_marginal and adh_ok_marginal:
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
