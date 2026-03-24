"""Standalone scoring evaluation orchestrator for Phase 23.

Gathers ground truth from Google Drive resume titles, selects a stratified
sample of jobs, re-scores with the corrected profile, then calls Claude Opus
three times to produce CODE_REVIEW.md, DATA_REVIEW.md, and RECOMMENDATIONS.md
in .planning/scoring_evaluation/.

Usage:
    python scoring_evaluator.py
    python scoring_evaluator.py --yes              # skip confirmation prompts
    python scoring_evaluator.py --skip-rescore     # use existing scores
    python scoring_evaluator.py --skip-drive       # skip ground truth from Drive

All API calls go through call_claude() with purpose='scoring_eval' for cost
tracking. Does NOT use Flask's g.db — creates its own sqlite3 connection per
stale_detector.py pattern.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

from job_finder.config import load_config
from job_finder.db import persist_haiku_score, persist_sonnet_score
from job_finder.web.claude_client import BudgetExceededError, call_claude, cost_gate
from job_finder.web.drive_uploader import get_drive_service
from job_finder.web.haiku_scorer import score_job_haiku
from job_finder.web.profile_schema import load_profile
from job_finder.web.sonnet_evaluator import evaluate_job_sonnet

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPUS_MODEL = "claude-opus-4-6"
OUTPUT_DIR = Path(".planning/scoring_evaluation")

# Score band boundaries
BAND_A_MIN = 70          # Strong match
BAND_B_MIN = 55          # Partial match (above threshold)
BAND_C_MIN = 40          # False negative zone (below threshold but not terrible)
BAND_D_MIN = 20          # Poor fit
# Band E: < 20

# Band limits for stratified sampling
BAND_LIMITS = {
    "A": 15,   # haiku_score >= 70
    "B": 20,   # haiku_score 55-69
    "C": 25,   # haiku_score 40-54 (false negative zone — oversample)
    "D": 15,   # haiku_score 20-39
    "E": 10,   # haiku_score < 20
}

# Placeholder indicator for profile quality check
_PLACEHOLDER_FRAGMENTS = [
    "your name",
    "example corp",
    "company name",
    "placeholder",
    "lorem ipsum",
    "insert",
    "acme",
]


# ---------------------------------------------------------------------------
# Stage 1: Profile quality guard
# ---------------------------------------------------------------------------

def check_profile_ready(
    profile_path: str = "experience_profile.json",
    config_path: str = "config.yaml",
) -> tuple[dict, dict]:
    """Verify profile and config contain real data before running evaluation.

    Args:
        profile_path: Path to experience_profile.json.
        config_path: Path to config.yaml.

    Returns:
        Tuple of (experience_profile dict, config dict).

    Exits with code 1 if profile or config contain placeholder data.
    """
    print("[1/7] Checking profile and config quality...")

    profile = load_profile(profile_path)
    config = load_config(config_path)

    errors = []

    # Check profile has real positions
    positions = profile.get("positions", [])
    if len(positions) <= 1:
        errors.append(
            f"experience_profile.json has only {len(positions)} position(s). "
            "A fully populated profile requires at least 2 positions."
        )
    else:
        # Check positions have achievements (not placeholder)
        has_achievements = all(
            bool(pos.get("achievements")) for pos in positions
        )
        if not has_achievements:
            errors.append(
                "One or more positions in experience_profile.json have no achievements. "
                "Populate all positions with real bullet points."
            )
        else:
            # Sanity check: achievements shouldn't be obvious placeholders
            all_achievements = [
                a.lower()
                for pos in positions
                for a in pos.get("achievements", [])
            ]
            for achievement in all_achievements:
                if any(frag in achievement for frag in _PLACEHOLDER_FRAGMENTS):
                    errors.append(
                        f"Placeholder text detected in achievements: '{achievement[:80]}...'"
                    )
                    break

    # Check config.yaml profile.target_titles has meaningful entries
    target_titles = config.get("profile", {}).get("target_titles", [])
    if len(target_titles) <= 1:
        errors.append(
            f"config.yaml profile.target_titles has only {len(target_titles)} entry. "
            "Add at least 2 real target job titles."
        )

    if errors:
        print("\n[ERROR] Profile data is not ready for scoring evaluation:")
        for err in errors:
            print(f"  - {err}")
        print(
            "\nPlease complete Phase 22 first:\n"
            "  1. Run the profile extractor to populate experience_profile.json\n"
            "  2. Populate config.yaml profile.target_titles with your real target titles\n"
            "  3. Re-run this script after completing those steps."
        )
        sys.exit(1)

    print(f"  Profile: {len(positions)} positions, "
          f"{sum(len(p.get('achievements', [])) for p in positions)} achievements")
    print(f"  Config:  {len(target_titles)} target title(s): {', '.join(target_titles[:3])}{'...' if len(target_titles) > 3 else ''}")
    print("  Profile quality check PASSED.")
    return profile, config


# ---------------------------------------------------------------------------
# Stage 2: Ground truth from Drive
# ---------------------------------------------------------------------------

def gather_ground_truth(
    conn: sqlite3.Connection,
    config: dict,
    skip_drive: bool = False,
) -> list[str]:
    """List Drive resume files and fuzzy-match them to job dedup_keys.

    Resume filenames encode role + company (e.g. "Senior Data Scientist -
    Acme Corp.docx"). Fuzzy-matched against jobs table to identify which jobs
    the user actually applied to.

    Args:
        conn: Open SQLite connection.
        config: Application config dict.
        skip_drive: If True, skip Drive lookup and return empty list.

    Returns:
        List of matched dedup_keys (jobs the user applied to).
    """
    if skip_drive:
        print("[2/7] Skipping Drive ground truth (--skip-drive flag).")
        return []

    print("[2/7] Gathering ground truth from Google Drive resume files...")

    try:
        from thefuzz import fuzz

        service = get_drive_service()
        folder_id = config.get("drive", {}).get("folder_id", "")

        if not folder_id:
            print("  Warning: drive.folder_id not set in config.yaml. Skipping ground truth.")
            return []

        # List files in the resumes folder
        results = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="files(id, name)",
                pageSize=200,
            )
            .execute()
        )
        files = results.get("files", [])

        if not files:
            print("  Warning: No files found in Drive resumes folder. Proceeding with empty ground truth.")
            return []

        print(f"  Found {len(files)} resume file(s) in Drive.")

        # Fetch all jobs from DB for fuzzy matching
        rows = conn.execute(
            "SELECT dedup_key, title, company FROM jobs WHERE haiku_score IS NOT NULL"
        ).fetchall()

        if not rows:
            print("  No scored jobs in DB — cannot match resume titles.")
            return []

        # Fuzzy-match each resume filename against job title+company
        matched_keys = []
        for file in files:
            filename = file["name"]
            # Strip extension
            stem = Path(filename).stem

            best_score = 0
            best_key = None
            for row in rows:
                job_str = f"{row['title']} {row['company']}"
                score = fuzz.token_set_ratio(stem, job_str)
                if score > best_score:
                    best_score = score
                    best_key = row["dedup_key"]

            if best_score >= 70 and best_key:
                matched_keys.append(best_key)
                print(f"  Matched: '{stem}' -> {best_key} (score={best_score})")

        # Deduplicate
        matched_keys = list(dict.fromkeys(matched_keys))
        print(f"  Ground truth: {len(matched_keys)} applied-to job(s) identified.")
        return matched_keys

    except (FileNotFoundError, ValueError) as e:
        print(f"  Warning: Drive unavailable ({e}). Proceeding with empty ground truth.")
        return []
    except Exception as e:
        print(f"  Warning: Could not gather ground truth from Drive: {e}. Proceeding.")
        return []


# ---------------------------------------------------------------------------
# Stage 3: Stratified sample selection
# ---------------------------------------------------------------------------

def select_stratified_sample(
    conn: sqlite3.Connection,
    ground_truth_keys: list[str],
) -> list[dict]:
    """Select a stratified sample of 50-100 jobs across 5 score bands.

    Band oversampling weights:
      A: haiku_score >= 70, limit 15
      B: haiku_score 55-69, limit 20
      C: haiku_score 40-54, limit 25 (false negative zone)
      D: haiku_score 20-39, limit 15
      E: haiku_score < 20, limit 10

    Ground truth jobs are always included regardless of band limits.

    Args:
        conn: Open SQLite connection.
        ground_truth_keys: dedup_keys of jobs the user applied to.

    Returns:
        List of job dicts from the sample.
    """
    print("[3/7] Selecting stratified sample...")

    # Query each band separately with RANDOM() ordering for variety
    bands = [
        ("A", f"haiku_score >= {BAND_A_MIN}", BAND_LIMITS["A"]),
        ("B", f"haiku_score >= {BAND_B_MIN} AND haiku_score < {BAND_A_MIN}", BAND_LIMITS["B"]),
        ("C", f"haiku_score >= {BAND_C_MIN} AND haiku_score < {BAND_B_MIN}", BAND_LIMITS["C"]),
        ("D", f"haiku_score >= {BAND_D_MIN} AND haiku_score < {BAND_C_MIN}", BAND_LIMITS["D"]),
        ("E", f"haiku_score < {BAND_D_MIN} AND haiku_score IS NOT NULL", BAND_LIMITS["E"]),
    ]

    seen_keys: set[str] = set()
    sample: list[dict] = []

    for band_name, condition, limit in bands:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {condition} ORDER BY RANDOM() LIMIT ?",
            (limit,),
        ).fetchall()
        count = 0
        for row in rows:
            key = row["dedup_key"]
            if key not in seen_keys:
                seen_keys.add(key)
                sample.append(dict(row))
                count += 1
        print(f"  Band {band_name}: {count} job(s)")

    # Ensure all ground truth jobs are in sample
    gt_added = 0
    for gt_key in ground_truth_keys:
        if gt_key not in seen_keys:
            row = conn.execute(
                "SELECT * FROM jobs WHERE dedup_key = ?", (gt_key,)
            ).fetchone()
            if row:
                seen_keys.add(gt_key)
                sample.append(dict(row))
                gt_added += 1

    if gt_added:
        print(f"  Added {gt_added} ground truth job(s) not already in sample.")

    print(f"  Total sample: {len(sample)} job(s)")
    return sample


# ---------------------------------------------------------------------------
# Stage 4: Re-score sample
# ---------------------------------------------------------------------------

def rescore_sample(
    client,
    conn: sqlite3.Connection,
    config: dict,
    experience_profile: dict,
    sample: list[dict],
    skip_confirmation: bool = False,
) -> list[dict]:
    """Re-score each sample job with Haiku (and Sonnet where eligible).

    Args:
        client: Anthropic client instance.
        conn: Open SQLite connection.
        config: Application config dict.
        experience_profile: experience_profile.json content.
        sample: List of job dicts to re-score.
        skip_confirmation: If True, skip cost confirmation prompt.

    Returns:
        Updated list of job dicts with fresh scores.
    """
    print("[4/7] Re-scoring sample jobs...")

    haiku_threshold = config.get("scoring", {}).get("haiku_threshold", 55)
    sonnet_eligible = sum(
        1 for j in sample
        if (j.get("haiku_score") or 0) >= haiku_threshold and j.get("jd_full")
    )

    # Estimate cost
    haiku_cost_est = len(sample) * 0.005
    sonnet_cost_est = sonnet_eligible * 0.02
    total_est = haiku_cost_est + sonnet_cost_est

    print(f"  Estimated re-scoring cost: ${total_est:.2f}")
    print(f"    ({len(sample)} Haiku calls @ ~$0.005 + {sonnet_eligible} Sonnet calls @ ~$0.02)")

    if not skip_confirmation:
        answer = input(f"  Continue with re-scoring? [y/n]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Skipping re-scoring.")
            return sample

    haiku_count = 0
    sonnet_count = 0
    updated_sample = []

    for i, job in enumerate(sample, 1):
        dedup_key = job["dedup_key"]

        # Haiku re-score — pass config as both profile and config (Haiku resolves profile sub-key)
        haiku_result = score_job_haiku(client, job, config, conn, config)
        if haiku_result:
            new_haiku_score = haiku_result.get("score")
            new_haiku_summary = haiku_result.get("summary", "")
            persist_haiku_score(conn, dedup_key, new_haiku_score, new_haiku_summary)
            job = dict(job)
            job["haiku_score"] = new_haiku_score
            job["haiku_summary"] = new_haiku_summary
            haiku_count += 1

            # Sonnet re-score if eligible
            if (
                new_haiku_score is not None
                and new_haiku_score >= haiku_threshold
                and job.get("jd_full")
            ):
                sonnet_result = evaluate_job_sonnet(
                    client, job, experience_profile, conn, config
                )
                if sonnet_result:
                    new_sonnet_score = sonnet_result.get("score")
                    new_fit_analysis = json.dumps(sonnet_result.get("fit_analysis", {}))
                    persist_sonnet_score(conn, dedup_key, new_sonnet_score, new_fit_analysis)
                    job["sonnet_score"] = new_sonnet_score
                    job["fit_analysis"] = new_fit_analysis
                    sonnet_count += 1

        updated_sample.append(job)
        if i % 10 == 0 or i == len(sample):
            print(f"  Re-scored {i}/{len(sample)} jobs (Haiku: {haiku_count}, Sonnet: {sonnet_count})")

    print(f"  Re-scoring complete: {haiku_count} Haiku, {sonnet_count} Sonnet calls.")
    return updated_sample


# ---------------------------------------------------------------------------
# Stage 5: Opus code review
# ---------------------------------------------------------------------------

def run_code_review(
    client,
    conn: sqlite3.Connection,
    config: dict,
) -> str:
    """Read scorer source files and call Opus for a code-quality evaluation.

    Reads haiku_scorer.py, sonnet_evaluator.py, profile_schema.py, and the
    scoring section of config.yaml, then asks Opus to identify prompt gaps,
    calibration issues, and profile utilization problems.

    Args:
        client: Anthropic client instance.
        conn: Open SQLite connection (for cost recording).
        config: Application config dict.

    Returns:
        Markdown content of CODE_REVIEW.md.
    """
    print("[5/7] Running Opus code review...")

    # Read source files
    haiku_src = Path("job_finder/web/haiku_scorer.py").read_text(encoding="utf-8")
    sonnet_src = Path("job_finder/web/sonnet_evaluator.py").read_text(encoding="utf-8")
    profile_schema_src = Path("job_finder/web/profile_schema.py").read_text(encoding="utf-8")

    # Read scoring config section
    scoring_cfg = config.get("scoring", {})
    scoring_yaml = json.dumps(scoring_cfg, indent=2)
    profile_cfg = config.get("profile", {})
    profile_yaml = json.dumps(profile_cfg, indent=2)

    system = (
        "You are a senior AI engineer performing a critical code review of a two-tier "
        "job scoring pipeline. Your goal is to identify concrete, actionable issues that "
        "cause good jobs (false negatives) to be scored too low or bad jobs (false positives) "
        "to be scored too high. Be specific — cite line numbers, prompt text, and exact "
        "configuration values. Produce a detailed CODE_REVIEW.md document."
    )

    user_message = (
        "Please review this two-tier AI scoring pipeline (Haiku fast-filter + Sonnet deep "
        "evaluation) for calibration issues, prompt gaps, and blind spots. Focus especially "
        "on false negatives — good jobs being missed.\n\n"
        "## Evaluation Focus Areas\n\n"
        "1. **Prompt gaps and calibration**: Are the system prompts well-calibrated? Do they "
        "correctly weight the scoring factors? Are there ambiguities that could cause "
        "inconsistent scores?\n\n"
        "2. **Profile utilization**: Haiku reads config.yaml profile section "
        "(target_titles, skills, industries, locations, min_salary). Sonnet reads "
        "experience_profile.json (positions with achievements, skills). Are both scorers "
        "making full use of the profile data available to them? What important data is unused?\n\n"
        "3. **Description snippet cutoff**: Haiku truncates description to 500 characters. "
        "Does this cause key qualifications to be missed? Provide a concrete assessment.\n\n"
        "4. **Haiku→Sonnet threshold**: The handoff threshold is currently "
        f"{config.get('scoring', {}).get('haiku_threshold', 55)}. Are good jobs being "
        "filtered out at this boundary? Is this threshold appropriate?\n\n"
        "5. **Unused scoring.weights**: config.yaml defines scoring.weights but they are "
        "not used by either scorer (freeform prompts instead). Evaluate whether structured "
        "weights would improve calibration vs the current freeform approach.\n\n"
        "6. **Profile schema completeness**: What important candidate attributes are missing "
        "from both profile schemas (seniority level, management vs IC preference, "
        "company size preference, remote-only preference, years of experience, etc.)?\n\n"
        "7. **Scoring dimensions missing**: What job attributes are the scorers blind to "
        "(company stage/size, growth trajectory, team structure, equity, culture signals)?\n\n"
        "8. **Sonnet output schema usefulness**: Are the fit_analysis fields (strengths, "
        "gaps, talking_points, resume_priority_skills) the right ones? Should they be "
        "restructured for better triage decisions?\n\n"
        "---\n\n"
        "## Haiku Scorer Source (haiku_scorer.py)\n\n"
        f"```python\n{haiku_src}\n```\n\n"
        "---\n\n"
        "## Sonnet Evaluator Source (sonnet_evaluator.py)\n\n"
        f"```python\n{sonnet_src}\n```\n\n"
        "---\n\n"
        "## Profile Schema Source (profile_schema.py — schema definition only)\n\n"
        f"```python\n{profile_schema_src[:3000]}\n```\n\n"
        "---\n\n"
        "## Scoring Config (from config.yaml)\n\n"
        f"```json\n{scoring_yaml}\n```\n\n"
        "## Profile Config (from config.yaml)\n\n"
        f"```json\n{profile_yaml}\n```\n\n"
        "---\n\n"
        "Please produce a thorough CODE_REVIEW.md document with clearly organized sections, "
        "specific findings with severity ratings (Critical/Important/Minor), and concrete "
        "explanations of the impact of each issue on scoring accuracy."
    )

    try:
        result, cost_usd = call_claude(
            client=client,
            model=OPUS_MODEL,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=None,
            conn=conn,
            job_id=None,
            purpose="scoring_eval",
            config=config,
            max_tokens=4096,
        )
        content = result.get("text", "")
        output_path = OUTPUT_DIR / "CODE_REVIEW.md"
        output_path.write_text(content, encoding="utf-8")
        print(f"  CODE_REVIEW.md written ({len(content)} chars, cost=${cost_usd:.4f})")
        return content
    except BudgetExceededError as e:
        print(f"  [ERROR] Budget exceeded for code review: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 6: Opus data review
# ---------------------------------------------------------------------------

def run_data_review(
    client,
    conn: sqlite3.Connection,
    config: dict,
    sample: list[dict],
    ground_truth_keys: list[str],
) -> str:
    """Serialize sample jobs and call Opus for a data quality evaluation.

    Args:
        client: Anthropic client instance.
        conn: Open SQLite connection (for cost recording).
        config: Application config dict.
        sample: List of job dicts from stratified sample.
        ground_truth_keys: dedup_keys of jobs the user applied to.

    Returns:
        Markdown content of DATA_REVIEW.md.
    """
    print("[6/7] Running Opus data review...")

    ground_truth_set = set(ground_truth_keys)

    # Serialize jobs to compact dicts — avoid full fit_analysis JSON (too many tokens)
    serialized_jobs = []
    for job in sample:
        fit_summary = {}
        raw_fit = job.get("fit_analysis")
        if raw_fit:
            try:
                parsed = json.loads(raw_fit) if isinstance(raw_fit, str) else raw_fit
                fit_summary = {
                    "strengths_count": len(parsed.get("strengths", [])),
                    "gaps_count": len(parsed.get("gaps", [])),
                    "strengths_preview": parsed.get("strengths", [])[:2],
                    "gaps_preview": parsed.get("gaps", [])[:2],
                }
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        entry: dict = {
            "dedup_key": job.get("dedup_key"),
            "title": job.get("title"),
            "company": job.get("company"),
            "haiku_score": job.get("haiku_score"),
            "sonnet_score": job.get("sonnet_score"),
            "haiku_summary": job.get("haiku_summary", "")[:200],
            "source": json.loads(job.get("sources") or "[]"),
            "applied_to": job.get("dedup_key") in ground_truth_set,
        }
        if fit_summary:
            entry["fit_summary"] = fit_summary
        serialized_jobs.append(entry)

    # Compute summary statistics
    haiku_scores = [j["haiku_score"] for j in serialized_jobs if j["haiku_score"] is not None]
    sonnet_scores = [j["sonnet_score"] for j in serialized_jobs if j["sonnet_score"] is not None]
    applied_jobs = [j for j in serialized_jobs if j["applied_to"]]

    haiku_threshold = config.get("scoring", {}).get("haiku_threshold", 55)

    def _percentile(scores: list[float], pct: float) -> float:
        if not scores:
            return 0.0
        sorted_s = sorted(scores)
        idx = int(len(sorted_s) * pct / 100)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    band_counts = {
        "A (>=70)": sum(1 for s in haiku_scores if s >= 70),
        "B (55-69)": sum(1 for s in haiku_scores if 55 <= s < 70),
        "C (40-54)": sum(1 for s in haiku_scores if 40 <= s < 55),
        "D (20-39)": sum(1 for s in haiku_scores if 20 <= s < 40),
        "E (<20)": sum(1 for s in haiku_scores if s < 20),
    }

    stats = {
        "total_sample": len(sample),
        "band_counts": band_counts,
        "ground_truth_count": len(applied_jobs),
        "haiku_min": min(haiku_scores) if haiku_scores else 0,
        "haiku_max": max(haiku_scores) if haiku_scores else 0,
        "haiku_p25": _percentile(haiku_scores, 25),
        "haiku_p50": _percentile(haiku_scores, 50),
        "haiku_p75": _percentile(haiku_scores, 75),
        "sonnet_count": len(sonnet_scores),
        "sonnet_p50": _percentile(sonnet_scores, 50) if sonnet_scores else 0,
        "haiku_threshold": haiku_threshold,
        "applied_avg_haiku": (
            sum(j["haiku_score"] or 0 for j in applied_jobs) / len(applied_jobs)
            if applied_jobs else 0
        ),
    }

    system = (
        "You are a data scientist performing a calibration analysis of a job scoring system. "
        "Your goal is to identify systematic scoring errors using a stratified sample of jobs "
        "and ground truth labels (jobs the user actually applied to). Focus on false negatives "
        "(good jobs scored too low) as the primary quality concern. Produce a detailed "
        "DATA_REVIEW.md document."
    )

    user_message = (
        "Please perform a data review of this stratified sample from a two-tier job scoring "
        "system. Focus heavily on whether the scoring pipeline is missing good jobs.\n\n"
        "## Summary Statistics\n\n"
        f"```json\n{json.dumps(stats, indent=2)}\n```\n\n"
        "## Evaluation Framework\n\n"
        "1. **False positive/negative quadrant analysis**: Classify jobs as:\n"
        "   - True positives: high-scored jobs the user applied to\n"
        "   - False positives: high-scored jobs the user did NOT apply to\n"
        "   - False negatives: low-scored jobs the user DID apply to\n"
        "   - True negatives: low-scored jobs the user did not apply to\n\n"
        "2. **Score distribution analysis**: Is the score distribution appropriate? "
        "Are scores concentrated in a narrow band? Is discrimination power adequate?\n\n"
        "3. **Haiku-vs-Sonnet agreement**: For jobs with both scores, do Haiku and Sonnet "
        "agree? Large disagreements indicate the fast filter is miscalibrated.\n\n"
        f"4. **Threshold boundary behavior**: At the {haiku_threshold} Haiku threshold, "
        "are there jobs with haiku_score near the threshold that likely deserve Sonnet "
        "evaluation? Are good jobs being filtered at this boundary?\n\n"
        "5. **Ground truth calibration**: Do the applied-to jobs (ground truth) score "
        "higher than non-applied jobs? If applied jobs cluster in Band C or lower, the "
        "scorer is significantly miscalibrated.\n\n"
        "6. **Source-agnostic quality**: Compare score distributions across job sources. "
        "Should scoring be source-agnostic or are there source-specific adjustments needed?\n\n"
        "---\n\n"
        "## Job Sample Data\n\n"
        f"```json\n{json.dumps(serialized_jobs, indent=2)}\n```\n\n"
        "---\n\n"
        "Please produce a thorough DATA_REVIEW.md with:\n"
        "- Executive summary of calibration quality\n"
        "- Quadrant analysis table (with specific job examples for each quadrant)\n"
        "- Score distribution findings\n"
        "- Haiku-Sonnet agreement analysis\n"
        "- Ground truth calibration assessment\n"
        "- Specific jobs that look like false negatives (explain why each is concerning)\n"
        "- Overall verdict: is the pipeline finding the right jobs?"
    )

    try:
        result, cost_usd = call_claude(
            client=client,
            model=OPUS_MODEL,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=None,
            conn=conn,
            job_id=None,
            purpose="scoring_eval",
            config=config,
            max_tokens=4096,
        )
        content = result.get("text", "")
        output_path = OUTPUT_DIR / "DATA_REVIEW.md"
        output_path.write_text(content, encoding="utf-8")
        print(f"  DATA_REVIEW.md written ({len(content)} chars, cost=${cost_usd:.4f})")
        return content
    except BudgetExceededError as e:
        print(f"  [ERROR] Budget exceeded for data review: {e}")
        raise


# ---------------------------------------------------------------------------
# Stage 7: Opus recommendations
# ---------------------------------------------------------------------------

def run_recommendations(
    client,
    conn: sqlite3.Connection,
    config: dict,
    code_review_content: str,
    data_review_content: str,
) -> str:
    """Synthesize CODE_REVIEW.md and DATA_REVIEW.md into concrete recommendations.

    Args:
        client: Anthropic client instance.
        conn: Open SQLite connection (for cost recording).
        config: Application config dict.
        code_review_content: Full text of CODE_REVIEW.md.
        data_review_content: Full text of DATA_REVIEW.md.

    Returns:
        Markdown content of RECOMMENDATIONS.md.
    """
    print("[7/7] Running Opus recommendations synthesis...")

    system = (
        "You are a senior AI engineer synthesizing a code review and data review into "
        "actionable recommendations for improving a job scoring pipeline. Produce concrete, "
        "ready-to-implement recommendations with exact text where applicable. The user will "
        "decide which recommendations to apply — your job is to make the decision as easy "
        "as possible by providing complete context and ready-to-paste content."
    )

    user_message = (
        "Based on the following code review and data review of a two-tier job scoring "
        "pipeline (Haiku fast-filter + Sonnet deep evaluation), produce a comprehensive "
        "RECOMMENDATIONS.md with concrete, actionable items.\n\n"
        "## Required Recommendation Format\n\n"
        "For each recommendation:\n"
        "1. **Title and severity tier**: Critical / Important / Nice-to-Have\n"
        "2. **Problem**: What issue does this fix and why does it matter\n"
        "3. **Ready-to-paste solution**: Exact revised prompt text, config.yaml changes, "
        "or code snippets — complete enough that the user can apply it directly\n"
        "4. **Cost impact**: Estimated monthly dollar delta based on ~300 jobs/month\n"
        "5. **Expected improvement**: Concrete prediction of how this changes scoring\n\n"
        "## Required Sections\n\n"
        "1. **Executive Summary**: Top 3 most impactful changes (1 paragraph)\n"
        "2. **Critical Recommendations**: Must-fix items with highest false-negative impact\n"
        "3. **Important Recommendations**: Meaningful improvements to calibration/accuracy\n"
        "4. **Nice-to-Have Recommendations**: Polish and edge cases\n"
        "5. **Prompt Rewrites**: Complete revised system prompts for both Haiku and Sonnet "
        "(ready to paste into haiku_scorer.py and sonnet_evaluator.py)\n"
        "6. **Config Recommendations**: Complete revised scoring section for config.yaml "
        "(weights, thresholds, new profile fields, model changes)\n"
        "7. **Profile Schema Recommendations**: New profile fields to add to "
        "experience_profile.json and config.yaml profile section\n"
        "8. **New Scoring Dimensions**: Blind spots to address in future iterations\n"
        "9. **Iterative Calibration Architecture**: Recommended approach for feeding "
        "triage decisions (user_interest, pipeline_status) back into scoring over time\n"
        "10. **Sonnet Output Schema Assessment**: Should the fit_analysis schema be "
        "restructured? What fields are most/least useful for triage?\n\n"
        "---\n\n"
        "## Code Review\n\n"
        f"{code_review_content}\n\n"
        "---\n\n"
        "## Data Review\n\n"
        f"{data_review_content}\n\n"
        "---\n\n"
        "Produce the complete RECOMMENDATIONS.md now. Be specific, be concrete, and make "
        "each recommendation independently actionable."
    )

    try:
        result, cost_usd = call_claude(
            client=client,
            model=OPUS_MODEL,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=None,
            conn=conn,
            job_id=None,
            purpose="scoring_eval",
            config=config,
            max_tokens=4096,
        )
        content = result.get("text", "")
        output_path = OUTPUT_DIR / "RECOMMENDATIONS.md"
        output_path.write_text(content, encoding="utf-8")
        print(f"  RECOMMENDATIONS.md written ({len(content)} chars, cost=${cost_usd:.4f})")
        return content
    except BudgetExceededError as e:
        print(f"  [ERROR] Budget exceeded for recommendations: {e}")
        raise


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    """Orchestrate the full scoring evaluation pipeline."""
    parser = argparse.ArgumentParser(
        description="Opus-powered scoring pipeline evaluation — Phase 23",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Stages:\n"
            "  1. Profile quality guard\n"
            "  2. Ground truth from Google Drive\n"
            "  3. Stratified sample selection\n"
            "  4. Re-score sample with corrected profile\n"
            "  5. Opus code review -> CODE_REVIEW.md\n"
            "  6. Opus data review -> DATA_REVIEW.md\n"
            "  7. Opus recommendations -> RECOMMENDATIONS.md\n"
        ),
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip all confirmation prompts (non-interactive mode)",
    )
    parser.add_argument(
        "--skip-rescore",
        action="store_true",
        help="Skip re-scoring step — use existing scores from DB",
    )
    parser.add_argument(
        "--skip-drive",
        action="store_true",
        help="Skip Google Drive ground truth lookup",
    )
    args = parser.parse_args()

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Stage 1: Profile quality guard
    experience_profile, config = check_profile_ready()

    # Open DB connection (standalone pattern — no Flask g.db)
    db_path = config.get("db", {}).get("path", "jobs.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    import anthropic
    client = anthropic.Anthropic()

    total_cost = 0.0

    try:
        # Stage 2: Ground truth
        ground_truth_keys = gather_ground_truth(conn, config, skip_drive=args.skip_drive)

        # Stage 3: Stratified sample
        sample = select_stratified_sample(conn, ground_truth_keys)
        if not sample:
            print("[ERROR] No scored jobs found in database. Run the scoring pipeline first.")
            sys.exit(1)

        # Stage 4: Re-score
        if not args.skip_rescore:
            sample = rescore_sample(
                client, conn, config, experience_profile, sample,
                skip_confirmation=args.yes,
            )
        else:
            print("[4/7] Skipping re-scoring (--skip-rescore flag).")

        # Check budget before Opus calls
        print("\nChecking budget before Opus calls (3 calls x ~$0.50-1.00 each)...")
        opus_allowed = cost_gate(conn, config, "sonnet")  # Opus uses same gate as sonnet
        if not opus_allowed:
            print("  [WARNING] Monthly budget cap is near or reached.")
            if not args.yes:
                answer = input("  Continue with Opus calls anyway? [y/n]: ").strip().lower()
                if answer not in ("y", "yes"):
                    print("  Aborting before Opus calls.")
                    sys.exit(0)
            else:
                print("  --yes flag set, continuing despite budget warning.")

        # Stage 5: Code review
        try:
            code_review = run_code_review(client, conn, config)
        except BudgetExceededError:
            print("  Budget exceeded — stopping before code review. Re-run after budget resets.")
            sys.exit(1)

        # Stage 6: Data review
        try:
            data_review = run_data_review(client, conn, config, sample, ground_truth_keys)
        except BudgetExceededError:
            print("  Budget exceeded — stopping before data review. Re-run after budget resets.")
            sys.exit(1)

        # Stage 7: Recommendations
        try:
            run_recommendations(client, conn, config, code_review, data_review)
        except BudgetExceededError:
            print("  Budget exceeded — stopping before recommendations. Re-run after budget resets.")
            sys.exit(1)

        # Print cost summary
        from job_finder.web.claude_client import get_cost_stats
        stats = get_cost_stats(conn)
        scoring_eval_spend = sum(
            row["spend"] for row in stats.get("by_feature", [])
            if row["purpose"] == "scoring_eval"
        )

        print("\n" + "=" * 60)
        print("SCORING EVALUATION COMPLETE")
        print("=" * 60)
        print(f"Output directory: {OUTPUT_DIR.resolve()}")
        print(f"  - CODE_REVIEW.md")
        print(f"  - DATA_REVIEW.md")
        print(f"  - RECOMMENDATIONS.md")
        print(f"\nScoring eval session cost: ${scoring_eval_spend:.4f}")
        print(f"Monthly total: ${stats.get('month', 0):.4f} / ${stats.get('budget_cap', 25):.2f}")
        print("=" * 60)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
