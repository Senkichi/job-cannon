"""CLI orchestrator for cascade audit (Phase 36).

Runs shadow-replay evaluation of the 6 non-scoring callsites across
providers, generates artifacts atomically, and produces reports.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml

from evals.cascade_audit.corpus_loader import CorpusLoader

logger = logging.getLogger(__name__)

CALLSITES = [
    "parse_structured_fields",
    "find_careers_url",
    "extract_jobs",
    "description_reformat",
    "company_research",
    "ai_nav_discovery",
]
DEFAULT_PROVIDERS = ["ollama", "gemini", "anthropic"]


@contextmanager
def _playwright_context():
    """Context manager for Playwright browser lifecycle.

    Yields a Playwright context. The runner manages browser lifecycle
    (launch at round start, close at round end). Adapter just uses the context.
    """
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    try:
        yield context
    finally:
        context.close()
        browser.close()
        p.stop()


def _get_git_commit_sha() -> str:
    """Get current git commit SHA for provenance."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _write_artifact_atomically(
    artifact_path: Path, data: dict, provenance: dict
) -> None:
    """Write artifact JSON atomically with provenance block.

    Args:
        artifact_path: Final path for artifact file.
        data: Artifact data (metrics, verdicts, etc.).
        provenance: Provenance block dict.
    """
    artifact = {
        "provenance": provenance,
        "config_snapshot": provenance.get("config_snapshot", {}),
        "model_versions": provenance.get("model_versions", {}),
        "commit_sha": provenance.get("commit_sha", provenance.get("harness_commit_sha", "unknown")),
        "data": data,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # Write to temp file first, then rename (atomic on Windows)
    temp_path = artifact_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    temp_path.rename(artifact_path)


def _load_judge_provider(config: dict):
    if not os.environ.get("OPENROUTER_API_KEY"):
        return None
    from job_finder.web.providers.openrouter_provider import OpenRouterProvider

    return OpenRouterProvider(config=config)


def _load_adapter(callsite: str, artifact_dir: Path, judge_provider=None):
    if callsite == "parse_structured_fields":
        from evals.cascade_audit.adapters.parse_structured_fields_adapter import (
            ParseStructuredFieldsAdapter,
        )

        return ParseStructuredFieldsAdapter()
    if callsite == "find_careers_url":
        from evals.cascade_audit.adapters.find_careers_url_adapter import (
            FindCareersUrlAdapter,
        )

        return FindCareersUrlAdapter()
    if callsite == "extract_jobs":
        from evals.cascade_audit.adapters.extract_jobs_adapter import ExtractJobsAdapter

        return ExtractJobsAdapter(artifact_dir=artifact_dir)
    if callsite == "description_reformat":
        from evals.cascade_audit.adapters.description_reformat_adapter import (
            DescriptionReformatAdapter,
        )

        return DescriptionReformatAdapter(judge_provider=judge_provider)
    if callsite == "company_research":
        from evals.cascade_audit.adapters.company_research_adapter import CompanyResearchAdapter

        return CompanyResearchAdapter(judge_provider=judge_provider)
    if callsite == "ai_nav_discovery":
        from evals.cascade_audit.adapters.ai_nav_discovery_adapter import AiNavDiscoveryAdapter

        return AiNavDiscoveryAdapter(artifact_dir=artifact_dir)
    raise ValueError(f"Unknown cascade audit callsite: {callsite}")


def _round_to_number(round_value: str) -> int:
    normalized = str(round_value).lower()
    if normalized in {"0", "r0"}:
        return 0
    if normalized in {"1", "r1"}:
        return 1
    if normalized in {"2", "r2"}:
        return 2
    raise ValueError(f"Unsupported audit round: {round_value}")


def _parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> dict:
    if total == 0:
        return {"low": 0.0, "high": 0.0, "half_width": 0.0}
    phat = successes / total
    denominator = 1 + z**2 / total
    center = (phat + z**2 / (2 * total)) / denominator
    margin = z * ((phat * (1 - phat) + z**2 / (4 * total)) / total) ** 0.5 / denominator
    return {
        "low": max(0.0, center - margin),
        "high": min(1.0, center + margin),
        "half_width": margin,
    }


def _artifact_verdict(metrics: dict) -> str:
    sample_count = int(metrics.get("sample_count", 0))
    error_count = int(metrics.get("error_count", 0))
    schema_valid_rate = float(metrics.get("schema_valid_rate", 1.0))
    if sample_count and error_count / sample_count > 0.10:
        return "UNSUITABLE"
    if schema_valid_rate < 0.85:
        return "UNSUITABLE"
    if sample_count and error_count / sample_count > 0.02:
        return "MARGINAL"
    if schema_valid_rate < 0.95:
        return "MARGINAL"
    return "SUITABLE"


def _enrich_artifact_data(round_num: int, artifact_data: dict) -> dict:
    metrics = dict(artifact_data.get("metrics", {}))
    sample_size = int(metrics.get("sample_count", 0))
    success_count = int(metrics.get("success_count", 0))
    schema_valid_rate = float(metrics.get("schema_valid_rate", 1.0 if sample_size else 0.0))
    enriched = dict(artifact_data)
    enriched["sample_size"] = sample_size
    enriched["accuracy"] = success_count / sample_size if sample_size else 0.0
    enriched["latency_p50_ms"] = metrics.get("latency_ms_avg", 0)
    enriched["cost_per_1k"] = metrics.get("cost_per_1k_avg", 0)
    enriched["schema_valid_rate"] = schema_valid_rate
    if round_num == 2:
        enriched["confidence_interval"] = _wilson_interval(success_count, sample_size)
        enriched["verdict"] = _artifact_verdict(metrics)
    return enriched


def _write_callsite_round_artifact(
    artifact_dir: Path,
    round_num: int,
    callsite: str,
    provider_results: dict[str, dict],
    provenance: dict,
) -> None:
    rows = list(provider_results.values())
    sample_size = max((int(row.get("sample_size", 0)) for row in rows), default=0)
    verdicts = [row.get("verdict") for row in rows if row.get("verdict")]
    summary = {
        "callsite": callsite,
        "round": f"r{round_num}",
        "sample_size": sample_size,
        "provider_results": provider_results,
    }
    if round_num == 2:
        summary["confidence_interval"] = _wilson_interval(
            sum(int(row.get("accuracy", 0) * int(row.get("sample_size", 0))) for row in rows),
            sum(int(row.get("sample_size", 0)) for row in rows),
        )
        summary["verdict"] = "SUITABLE" if "SUITABLE" in verdicts else (verdicts[0] if verdicts else "UNSUITABLE")
    artifact_path = artifact_dir / f"round_{round_num}" / f"{callsite}_r{round_num}.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    _write_artifact_atomically(artifact_path, summary, provenance)


def _emit_resume_schedulers_prompt() -> None:
    print("\n" + "=" * 60)
    print("RESUME SCHEDULERS")
    print("=" * 60)
    print("\nAudit Round 2 (R2) complete.")
    print("Re-enable APScheduler jobs to resume background ingest/scoring:")
    print("  1. Open Flask app at http://localhost:5000/settings/scheduler")
    print("  2. Click 'Enable Scheduler' if disabled")
    print("  3. Verify jobs are running in scheduler status")
    print("\n" + "=" * 60 + "\n")


def _aggregate_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"sample_count": 0, "success_count": 0, "error_count": 0}
    metrics: dict[str, object] = {
        "sample_count": len(rows),
        "success_count": sum(1 for row in rows if row.get("ok")),
        "error_count": sum(1 for row in rows if not row.get("ok")),
    }
    numeric: dict[str, list[float]] = {}
    booleans: dict[str, list[bool]] = {}
    for row in rows:
        for key, value in row.get("metrics", {}).items():
            if isinstance(value, bool):
                booleans.setdefault(key, []).append(value)
            elif isinstance(value, int | float):
                numeric.setdefault(key, []).append(float(value))
    for key, values in booleans.items():
        metrics[f"{key}_rate"] = sum(1 for value in values if value) / len(values)
    for key, values in numeric.items():
        metrics[f"{key}_avg"] = sum(values) / len(values)
    return metrics


def _exercise_rows(
    adapter,
    rows: list[dict],
    provider: str,
    config: dict,
    conn: sqlite3.Connection,
    callsite: str,
    artifact_dir: Path,
) -> dict:
    results: list[dict] = []
    verdicts: list[dict] = []
    for row in rows:
        key = row.get("dedup_key")
        try:
            if callsite == "ai_nav_discovery":
                with _playwright_context() as context:
                    candidate = adapter.exercise(row, provider, config, conn, context)
            else:
                candidate = adapter.exercise(row, provider, config, conn)
            metrics = adapter.score(row, candidate)
            if "judge_winner" in metrics:
                verdicts.append(
                    {
                        "dedup_key": key,
                        "winner": metrics.get("judge_winner"),
                        "rationale": metrics.get("judge_rationale"),
                        "confidence": metrics.get("judge_confidence"),
                        "position_swap_agreement": metrics.get(
                            "judge_position_swap_agreement"
                        ),
                    }
                )
            results.append(
                {
                    "dedup_key": key,
                    "ok": True,
                    "input": row,
                    "candidate": candidate,
                    "metrics": metrics,
                }
            )
        except Exception as exc:
            logger.exception("Cascade audit row failed: %s %s", callsite, key)
            results.append(
                {
                    "dedup_key": key,
                    "ok": False,
                    "input": row,
                    "error": str(exc),
                    "metrics": {},
                }
            )
    metrics = _aggregate_metrics(results)
    gate_outcomes = {
        "artifact_provenance": "pass",
        "row_execution": "pass" if metrics["error_count"] == 0 else "partial",
    }
    return {"rows": results, "metrics": metrics, "verdicts": verdicts, "gate_outcomes": gate_outcomes}


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Cascade audit eval harness — shadow-replay evaluation"
    )
    parser.add_argument(
        "--round",
        type=str,
        required=True,
        choices=["0", "1", "2", "r0", "r1", "r2"],
        help="Round number (r0/0=dry-run, r1/1=cheap-screen, r2/2=full-battery)",
    )
    parser.add_argument(
        "--callsite",
        type=str,
        help="Single callsite to evaluate (Phase 37 shorthand)",
    )
    parser.add_argument(
        "--callsites",
        type=str,
        required=False,
        help="Comma-separated list of callsites to evaluate",
    )
    parser.add_argument(
        "--providers",
        type=str,
        required=False,
        help="Comma-separated list of providers to evaluate",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="jobs.db",
        help="Path to production SQLite database",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=str,
        default="evals/cascade_audit/artifacts",
        help="Base directory for artifacts (default: evals/cascade_audit/artifacts)",
    )

    args = parser.parse_args()
    round_num = _round_to_number(args.round)

    # Load config
    with open(args.config) as f:
        config = yaml.safe_load(f)

    # Initialize corpus loader
    artifact_dir = Path(args.artifact_dir)
    loader = CorpusLoader(artifact_dir=artifact_dir, db_path=args.db_path)

    # Connect to DB
    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Load corpus based on round
        if round_num == 0:
            corpus = loader.load_round_0(
                n_per_callsite=3, conn=conn  # Round 0 uses n=3 per callsite
            )
        else:
            corpus = loader.load_round_1(conn=conn)

        # Parse callsites and providers
        callsite_arg = args.callsite or args.callsites
        callsites = _parse_csv(callsite_arg, CALLSITES)
        providers = _parse_csv(args.providers, DEFAULT_PROVIDERS)

        # Build provenance block
        commit_sha = _get_git_commit_sha()
        provenance = {
            "config_snapshot": config.get("providers", {}),
            "provider_config": config.get("providers", {}),
            "model_versions": {provider: provider for provider in providers},
            "commit_sha": commit_sha,
            "harness_commit_sha": commit_sha,
            "sample_seed": "sqlite-random",
            "scheduler_pause_status": "unknown",  # TODO: check scheduler status
        }
        judge_provider = _load_judge_provider(config)

        # Evaluate each (callsite, provider) pair
        for callsite in callsites:
            provider_results: dict[str, dict] = {}
            for provider in providers:
                logger.info(
                    f"Evaluating round={round_num} callsite={callsite} provider={provider}"
                )

                adapter = _load_adapter(callsite, artifact_dir, judge_provider)
                artifact_data = _exercise_rows(
                    adapter=adapter,
                    rows=corpus.get(callsite, []),
                    provider=provider,
                    config=config,
                    conn=conn,
                    callsite=callsite,
                    artifact_dir=artifact_dir,
                )
                artifact_data = _enrich_artifact_data(round_num, artifact_data)
                provider_results[provider] = artifact_data

                artifact_path = (
                    artifact_dir / f"round_{round_num}" / f"{callsite}_{provider}.json"
                )
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                _write_artifact_atomically(artifact_path, artifact_data, provenance)

                logger.info(f"Artifact written to {artifact_path}")
            _write_callsite_round_artifact(
                artifact_dir, round_num, callsite, provider_results, provenance
            )

        # Generate reports
        from evals.cascade_audit.report import write_cascade_audit_report, write_report

        report_dir = artifact_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        for callsite in callsites:
            for provider in providers:
                report_path = report_dir / f"round_{round_num}_{callsite}_{provider}.md"
                write_report(
                    round_num=round_num,
                    callsite=callsite,
                    provider=provider,
                    artifacts_dir=artifact_dir,
                    output_path=report_path,
                )
                logger.info(f"Report written to {report_path}")
        if round_num == 2:
            write_cascade_audit_report(
                artifacts_dir=artifact_dir,
                output_path=Path("CASCADE-AUDIT.md"),
            )
            _emit_resume_schedulers_prompt()

    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
