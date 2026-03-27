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
from job_finder.web.sonnet_evaluator import SONNET_SCHEMA, _SYSTEM_PROMPT


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


# ---------------------------------------------------------------------------
# run_eval orchestrator
# ---------------------------------------------------------------------------


def run_eval(
    provider: str,
    model: str | None,
    sample_size: int,
    thresholds: dict,
    skip_confirm: bool = False,
) -> None:
    """Orchestrate a full provider evaluation: sample, call, measure, report.

    Args:
        provider: Provider name to evaluate ("gemini" or "ollama").
        model: Model identifier override. If None, use config's default for sonnet tier.
        sample_size: Number of jobs to sample from the DB.
        thresholds: Dict with keys correlation_suitable, correlation_marginal,
                    adherence_suitable, adherence_marginal.
        skip_confirm: If True, skip the confirmation prompt.
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
        jobs = sample_jobs(conn, sample_size)

        if len(jobs) == 0:
            print("No Sonnet-scored jobs found. Run batch Sonnet scoring first.")
            sys.exit(1)

        # 4. Confirmation prompt
        if not skip_confirm:
            print(f"\nEvaluation Plan:")
            print(f"  Provider : {provider}")
            print(f"  Model    : {eval_model}")
            print(f"  Jobs     : {len(jobs)}")
            answer = input("\nProceed? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)

        # 5. Per-job evaluation loop
        per_job_results: list[dict] = []
        total = len(jobs)

        for i, job in enumerate(jobs):
            system, user_message = reconstruct_prompt(job, experience_profile, config)
            messages = [{"role": "user", "content": user_message}]

            t0 = time.perf_counter()
            eval_score: float | None = None
            schema_valid = False
            error: str | None = None

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
            except Exception as exc:
                error = str(exc)

            latency = time.perf_counter() - t0

            baseline = job["sonnet_score"]
            print(
                f"[{i + 1}/{total}] {job['title']} @ {job['company']} "
                f"— score: {eval_score} (baseline: {baseline}) {latency:.1f}s"
                + (f" [ERROR: {error}]" if error else "")
            )

            per_job_results.append({
                "dedup_key": job["dedup_key"],
                "title": job["title"],
                "company": job["company"],
                "baseline_score": baseline,
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
        )

        # 7. Build and save report
        report = {
            "meta": {
                "provider": provider,
                "model": eval_model,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sample_size": len(jobs),
                "db_path": config["db"]["path"],
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

    print("\n" + "=" * 60)
    print(f"VERDICT: {verdict_display}")
    print("=" * 60)
    print(f"  Score correlation (Pearson r) : {corr_str}")
    print(f"  Schema adherence rate         : {metrics['schema_adherence_rate']:.1%}")
    print(f"  Median latency                : {metrics['median_latency_seconds']:.1f}s")
    print(f"  Mean latency                  : {metrics['mean_latency_seconds']:.1f}s")
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
        choices=["gemini", "ollama"],
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
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt (non-interactive mode).",
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
    }
    run_eval(
        provider=args.provider,
        model=args.model,
        sample_size=args.sample_size,
        thresholds=thresholds,
        skip_confirm=args.yes,
    )


if __name__ == "__main__":
    main()
