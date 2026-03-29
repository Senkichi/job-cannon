"""Score jobs via Claude Opus CLI to establish gold-standard eval baselines.

Uses `claude -p` (non-interactive mode) to score jobs through the user's
Claude Max subscription rather than pay-per-token API billing.

Features:
- Stratified sampling: equal representation across score buckets
- Batch/pause support: process N jobs, pause for budget check, continue
- Resume support: skip already-scored jobs (--resume)
- Stores opus_score in DB for use as eval baseline

Usage:
    python opus_baseline.py --sample-size 50 --batch-size 10
    python opus_baseline.py --sample-size 50 --resume --yes --no-pause
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_finder.config import load_config
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.profile_schema import load_profile
from job_finder.web.sonnet_evaluator import SONNET_SCHEMA, _SYSTEM_PROMPT
from eval_provider import reconstruct_prompt, save_report


# Windows cp1252 console can't handle unicode in job titles (emojis, etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_CMD = "claude"

# JSON output instruction appended to system prompt for CLI invocation.
# claude -p lacks tool_use JSON enforcement, so we must instruct explicitly.
_JSON_SUFFIX = (
    "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object. "
    "No markdown, no explanation, no code fences. Just the raw JSON.\n"
    "Required schema: {\"score\": <integer 0-100>, \"summary\": <string>, "
    "\"fit_analysis\": {\"strengths\": [<string>], \"gaps\": [<string>], "
    "\"talking_points\": [<string>], \"resume_priority_skills\": [<string>]}}"
)

SCORE_BUCKETS: list[tuple[int, int]] = [
    (0, 19),
    (20, 39),
    (40, 59),
    (60, 79),
    (80, 100),
]


# ---------------------------------------------------------------------------
# stratified_sample_jobs
# ---------------------------------------------------------------------------


def stratified_sample_jobs(
    conn: Any,
    n: int,
    skip_scored: bool = False,
) -> list[dict]:
    """Sample jobs with equal representation across Sonnet score buckets.

    Divides the score range into 5 buckets and samples n // 5 jobs per
    bucket. If a bucket has fewer jobs than the per-bucket quota, takes
    all available and redistributes unused slots to larger buckets.

    Args:
        conn: Open sqlite3 connection with row_factory=sqlite3.Row.
        n: Total number of jobs to sample.
        skip_scored: If True, exclude jobs that already have opus_score.

    Returns:
        List of job dicts with keys matching sample_jobs() output plus
        opus_score.
    """
    per_bucket = n // len(SCORE_BUCKETS)
    remainder = n % len(SCORE_BUCKETS)

    scored_filter = " AND opus_score IS NULL" if skip_scored else ""

    # First pass: sample per_bucket from each bucket
    results: list[dict] = []
    shortfall = 0

    for i, (lo, hi) in enumerate(SCORE_BUCKETS):
        quota = per_bucket + (1 if i < remainder else 0)
        rows = conn.execute(
            "SELECT dedup_key, title, company, location, "
            "salary_min, salary_max, jd_full, sonnet_score, "
            "fit_analysis, haiku_score, opus_score "
            "FROM jobs "
            "WHERE sonnet_score IS NOT NULL AND jd_full IS NOT NULL "
            f"AND sonnet_score >= ? AND sonnet_score <= ?{scored_filter} "
            "ORDER BY RANDOM() LIMIT ?",
            (lo, hi, quota),
        ).fetchall()

        bucket_jobs = [dict(row) for row in rows]
        results.extend(bucket_jobs)

        fetched = len(bucket_jobs)
        if fetched < quota:
            shortfall += quota - fetched
            print(f"  Bucket {lo}-{hi}: {fetched}/{quota} (underflow)")
        else:
            print(f"  Bucket {lo}-{hi}: {fetched}/{quota}")

    # Second pass: fill shortfall from largest available bucket
    if shortfall > 0:
        existing_keys = {r["dedup_key"] for r in results}
        placeholders = ",".join("?" * len(existing_keys))
        fill_rows = conn.execute(
            "SELECT dedup_key, title, company, location, "
            "salary_min, salary_max, jd_full, sonnet_score, "
            "fit_analysis, haiku_score, opus_score "
            "FROM jobs "
            "WHERE sonnet_score IS NOT NULL AND jd_full IS NOT NULL "
            f"AND dedup_key NOT IN ({placeholders}){scored_filter} "
            "ORDER BY RANDOM() LIMIT ?",
            (*existing_keys, shortfall),
        ).fetchall()
        results.extend(dict(row) for row in fill_rows)
        if fill_rows:
            print(f"  Filled {len(fill_rows)} shortfall slots from remaining pool")

    return results


# ---------------------------------------------------------------------------
# call_opus_cli
# ---------------------------------------------------------------------------


def call_opus_cli(
    system_prompt: str,
    user_message: str,
    timeout_seconds: int = 120,
) -> tuple[dict | None, str | None]:
    """Call Claude Opus via `claude -p` CLI and parse structured output.

    Pipes the user message via stdin to avoid Windows argument length limits.
    Parses the JSON envelope from --output-format json and extracts the
    model's scoring response.

    Args:
        system_prompt: System prompt for scoring.
        user_message: Full user message with job details and profile.
        timeout_seconds: Per-job timeout in seconds.

    Returns:
        Tuple of (parsed_data, error). On success, parsed_data is a dict
        with score/summary/fit_analysis keys and error is None. On failure,
        parsed_data is None and error is a description string.
    """
    cmd = [
        CLAUDE_CMD, "-p",
        "--model", "opus",
        "--output-format", "json",
        "--tools", "",
        "--system-prompt", system_prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            input=user_message,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return None, f"Timeout after {timeout_seconds}s"

    if result.returncode != 0:
        stderr = result.stderr.strip()[:200] if result.stderr else "unknown error"
        return None, f"Exit code {result.returncode}: {stderr}"

    # Parse the Claude Code JSON envelope
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, f"Invalid JSON envelope: {result.stdout[:200]}"

    if envelope.get("is_error"):
        return None, f"Claude error: {envelope.get('result', 'unknown')[:200]}"

    raw_result = envelope.get("result", "")

    # Extract JSON from the model's response (may be wrapped in ```json fences)
    return _parse_model_json(raw_result)


def _parse_model_json(raw: str) -> tuple[dict | None, str | None]:
    """Extract and parse JSON from model output, handling markdown fences.

    Args:
        raw: Raw model output string.

    Returns:
        Tuple of (parsed_dict, error_string).
    """
    # Try direct parse first
    try:
        data = json.loads(raw)
        return data, None
    except (json.JSONDecodeError, TypeError):
        pass

    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            return data, None
        except json.JSONDecodeError:
            pass

    # Try to find any JSON object in the response
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data, None
        except json.JSONDecodeError:
            pass

    return None, f"No valid JSON found in response: {raw[:200]}"


# ---------------------------------------------------------------------------
# store_opus_score
# ---------------------------------------------------------------------------


def store_opus_score(conn: Any, dedup_key: str, score: float) -> None:
    """Store an Opus baseline score in the jobs table.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: Job identifier.
        score: Opus evaluation score (0-100).
    """
    conn.execute(
        "UPDATE jobs SET opus_score = ? WHERE dedup_key = ?",
        (score, dedup_key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# run_opus_baseline
# ---------------------------------------------------------------------------


def run_opus_baseline(
    sample_size: int = 50,
    batch_size: int = 10,
    skip_confirm: bool = False,
    resume: bool = False,
    no_pause: bool = False,
    timeout: int = 120,
) -> None:
    """Orchestrate Opus baseline scoring: sample, score via CLI, store.

    Args:
        sample_size: Number of jobs to sample.
        batch_size: Number of jobs per batch before pausing.
        skip_confirm: If True, skip initial confirmation prompt.
        resume: If True, skip already-scored jobs (opus_score IS NOT NULL).
        no_pause: If True, skip batch pauses (unattended mode).
        timeout: Per-job timeout in seconds for claude -p.
    """
    # 1. Load config and profile
    config = load_config()
    experience_profile = load_profile()

    # 2. Sample jobs
    with standalone_connection(config["db"]["path"]) as conn:
        print("Sampling jobs (stratified by Sonnet score bucket):")
        jobs = stratified_sample_jobs(conn, sample_size, skip_scored=resume)

        if not jobs:
            print("No eligible jobs found." + (" All may be scored already." if resume else ""))
            sys.exit(1)

        total = len(jobs)
        print(f"\nOpus Baseline Plan:")
        print(f"  Jobs      : {total} (requested {sample_size})")
        print(f"  Batch     : {batch_size}")
        print(f"  Timeout   : {timeout}s per job")
        print(f"  Resume    : {resume}")

        # 3. Confirmation
        if not skip_confirm:
            answer = input("\nProceed? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)

        # 4. Per-job scoring loop with batching
        per_job_results: list[dict] = []
        scored = 0
        errors = 0
        total_batches = (total + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch_end = min(batch_start + batch_size, total)
            batch = jobs[batch_start:batch_end]

            for i, job in enumerate(batch):
                overall_idx = batch_start + i + 1

                system, user_msg = reconstruct_prompt(job, experience_profile, config)
                system += _JSON_SUFFIX

                t0 = time.perf_counter()
                data, error = call_opus_cli(system, user_msg, timeout)
                latency = time.perf_counter() - t0

                opus_score = None
                schema_valid = False

                if data and isinstance(data.get("score"), (int, float)):
                    opus_score = data["score"]
                    schema_valid = True
                    store_opus_score(conn, job["dedup_key"], opus_score)
                    scored += 1
                else:
                    errors += 1
                    if error is None:
                        error = "Missing or invalid 'score' in response"

                sonnet = job["sonnet_score"]
                score_str = str(opus_score) if opus_score is not None else "None"
                print(
                    f"[{overall_idx}/{total}] {job['title']} @ {job['company']} "
                    f"- opus: {score_str} (sonnet: {sonnet}) {latency:.1f}s"
                    + (f" [ERROR: {error}]" if error else "")
                )

                per_job_results.append({
                    "dedup_key": job["dedup_key"],
                    "title": job["title"],
                    "company": job["company"],
                    "sonnet_score": sonnet,
                    "opus_score": opus_score,
                    "schema_valid": schema_valid,
                    "error": error,
                    "latency_seconds": latency,
                })

            # Batch pause
            batch_num = batch_idx + 1
            print(
                f"\n--- Batch {batch_num}/{total_batches} complete. "
                f"{scored} scored, {errors} errors out of {batch_end} processed. ---"
            )

            if not no_pause and batch_end < total:
                answer = input("Continue? [Y/n]: ").strip().lower()
                if answer in ("n", "no"):
                    print("Paused. Re-run with --resume to continue.")
                    break

        # 5. Save report
        report = {
            "meta": {
                "provider": "opus_baseline",
                "model": "claude-opus-4-6",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sample_size": total,
                "scored": scored,
                "errors": errors,
                "db_path": config["db"]["path"],
            },
            "per_job": per_job_results,
        }

        report_path = save_report(report, "opus_baseline")

        # 6. Summary
        print(f"\n{'=' * 60}")
        print(f"OPUS BASELINE COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Scored    : {scored}/{total}")
        print(f"  Errors    : {errors}")
        print(f"  Report    : {report_path}")

        # Quick sonnet vs opus correlation for scored jobs
        if scored >= 5:
            from statistics import correlation
            pairs = [
                (r["sonnet_score"], r["opus_score"])
                for r in per_job_results
                if r["opus_score"] is not None and r["sonnet_score"] is not None
            ]
            if len(pairs) >= 2:
                sonnet_scores = [p[0] for p in pairs]
                opus_scores = [p[1] for p in pairs]
                try:
                    r = correlation(sonnet_scores, opus_scores)
                    print(f"  Sonnet-Opus r : {r:.3f}")
                except Exception:
                    pass

        print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for opus_baseline."""
    parser = argparse.ArgumentParser(
        prog="opus_baseline",
        description="Score jobs via Claude Opus CLI to establish eval baseline.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=50,
        metavar="N",
        help="Number of jobs to sample (default: 50).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        metavar="N",
        help="Jobs per batch before pausing (default: 10).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="SECS",
        help="Per-job timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip jobs that already have opus_score.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip initial confirmation prompt.",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Skip batch pauses (unattended mode).",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point for opus_baseline CLI."""
    args = parse_args()
    run_opus_baseline(
        sample_size=args.sample_size,
        batch_size=args.batch_size,
        skip_confirm=args.yes,
        resume=args.resume,
        no_pause=args.no_pause,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
