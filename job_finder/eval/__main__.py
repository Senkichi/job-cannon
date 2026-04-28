"""CLI entry: ``python -m job_finder.eval [flags]``.

Three usage modes (driven by which flags you pass):

    diagnose:   python -m job_finder.eval --variant <name> --runs 3
    A/B:        python -m job_finder.eval --variant <name> --baseline <run_id>
    regression: python -m job_finder.eval --variant baseline --runs 3

Always prints the report path on success so callers can pipe to
``cat`` / ``code`` without an extra DB query.
"""

from __future__ import annotations

import argparse
import sys

import yaml

from job_finder.eval.harness import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m job_finder.eval",
        description="Run a scoring variant against the gold set and write an eval report.",
    )
    parser.add_argument(
        "--db",
        default="jobs.db",
        help="Path to the SQLite jobs DB (default: jobs.db)",
    )
    parser.add_argument(
        "--variant",
        default="baseline",
        help=(
            "Variant module name. 'baseline' aliases the production "
            "v3_scoring_prompt; otherwise resolved as "
            "scoring_prompts.variants.<name> (default: baseline)"
        ),
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=(
            "run_id of a previous eval_runs row to compare against (A/B mode). "
            "Omit for diagnose mode."
        ),
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of scoring calls per gold-set row (default: 3)",
    )
    parser.add_argument(
        "--report-dir",
        default=".planning/eval_results",
        help="Directory to write the markdown report into",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the application config (default: config.yaml)",
    )
    args = parser.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    report_path = run(
        db_path=args.db,
        variant_name=args.variant,
        n_runs=args.runs,
        baseline_run_id=args.baseline,
        report_dir=args.report_dir,
        config=config,
    )
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
