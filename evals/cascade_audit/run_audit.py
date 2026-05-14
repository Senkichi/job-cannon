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
        type=int,
        required=True,
        choices=[0, 1, 2],
        help="Round number (0=dry-run, 1=cheap-screen, 2=full-battery)",
    )
    parser.add_argument(
        "--callsites",
        type=str,
        required=True,
        help="Comma-separated list of callsites to evaluate",
    )
    parser.add_argument(
        "--providers",
        type=str,
        required=True,
        help="Comma-separated list of providers to evaluate",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        required=True,
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
        if args.round == 0:
            corpus = loader.load_round_0(
                n_per_callsite=3, conn=conn  # Round 0 uses n=3 per callsite
            )
        else:
            corpus = loader.load_round_1(conn=conn)

        # Parse callsites and providers
        callsites = [c.strip() for c in args.callsites.split(",")]
        providers = [p.strip() for p in args.providers.split(",")]

        # Build provenance block
        provenance = {
            "provider_config": config.get("providers", {}),
            "model_versions": {provider: provider for provider in providers},
            "harness_commit_sha": _get_git_commit_sha(),
            "sample_seed": "sqlite-random",
            "scheduler_pause_status": "unknown",  # TODO: check scheduler status
        }
        judge_provider = _load_judge_provider(config)

        # Evaluate each (callsite, provider) pair
        for callsite in callsites:
            for provider in providers:
                logger.info(
                    f"Evaluating round={args.round} callsite={callsite} provider={provider}"
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

                artifact_path = (
                    artifact_dir / f"round_{args.round}" / f"{callsite}_{provider}.json"
                )
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                _write_artifact_atomically(artifact_path, artifact_data, provenance)

                logger.info(f"Artifact written to {artifact_path}")

        # Generate reports
        from evals.cascade_audit.report import write_report

        report_dir = artifact_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        for callsite in callsites:
            for provider in providers:
                report_path = report_dir / f"round_{args.round}_{callsite}_{provider}.md"
                write_report(
                    round_num=args.round,
                    callsite=callsite,
                    provider=provider,
                    artifacts_dir=artifact_dir,
                    output_path=report_path,
                )
                logger.info(f"Report written to {report_path}")

    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
