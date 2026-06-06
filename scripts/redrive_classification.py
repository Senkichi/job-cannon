"""Phase 49.04 — reconcile stored ``classification`` with the current rule.

``derive_classification`` is the Python source of truth for the 5-way
classification. Over time the rule evolves (e.g. the ``low_signal`` branch was
added later), leaving a small fraction of rows (~3.5%, F-11) with a stored
``classification`` that no longer matches what the rule produces today.

This one-shot, idempotent script re-derives the classification for every
LLM-scored row (``scoring_model IS NOT NULL`` — the D-17 discriminator) and,
for divergent rows, rewrites it through the SOLE sanctioned writer
``persist_job_assessment`` (NOT a raw UPDATE — the m078 I-05 trigger and the
singleton CI gate both depend on that single path).

Modes:
    --audit      dry run: report divergence count + up to 5 example rows. No writes.
    --remediate  (default) rewrite divergent rows via persist_job_assessment.
    --verify     re-run the audit; exit 0 iff divergence is zero, else 1.

Usage:
    uv run --active python scripts/redrive_classification.py --audit
    uv run --active python scripts/redrive_classification.py --remediate
    uv run --active python scripts/redrive_classification.py --verify
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass

from job_finder.db import JobAssessment, derive_classification, persist_job_assessment

_LLM_SCORED_SQL = """
    SELECT dedup_key, classification, sub_scores_json, fit_analysis,
           scoring_provider, legitimacy_note, enrichment_tier,
           COALESCE(LENGTH(jd_full), 0) AS jd_len
    FROM jobs
    WHERE scoring_model IS NOT NULL
"""


@dataclass(frozen=True)
class Divergence:
    """A row whose stored classification differs from the current rule."""

    dedup_key: str
    stored: str | None
    computed: str


def _threshold(config: dict | None) -> int:
    if config is None:
        return 1500
    scoring_cfg = config.get("scoring") or {}
    return int(scoring_cfg.get("low_signal_jd_chars", 1500))


def find_divergences(conn: sqlite3.Connection, config: dict | None = None) -> list[Divergence]:
    """Return every LLM-scored row whose stored classification != the current rule.

    Rows with empty/unparseable sub_scores are skipped (no rule input → nothing
    to reconcile).
    """
    threshold = _threshold(config)
    out: list[Divergence] = []
    for row in conn.execute(_LLM_SCORED_SQL).fetchall():
        dedup_key, stored, sub_json, _fit, _provider, legit, tier, jd_len = row
        try:
            sub_scores = json.loads(sub_json) if sub_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(sub_scores, dict) or not sub_scores:
            continue
        computed = derive_classification(
            sub_scores,
            legit,
            enrichment_tier=tier,
            jd_full_length=jd_len or 0,
            low_signal_threshold=threshold,
        )
        if computed != stored:
            out.append(Divergence(dedup_key=dedup_key, stored=stored, computed=computed))
    return out


def remediate(conn: sqlite3.Connection, config: dict | None = None) -> int:
    """Rewrite every divergent row through persist_job_assessment. Returns count.

    Reconstructs the JobAssessment from the stored sub_scores + rationale and
    passes provider=None/model=None so the existing scoring_provider/scoring_model
    are preserved (COALESCE no-ops). persist_job_assessment re-derives the
    classification authoritatively and writes the canonical tuple.
    """
    divergences = find_divergences(conn, config)
    for d in divergences:
        row = conn.execute(
            "SELECT sub_scores_json, fit_analysis, scoring_provider FROM jobs WHERE dedup_key = ?",
            (d.dedup_key,),
        ).fetchone()
        if row is None:
            continue
        sub_json, fit, provider = row
        try:
            sub_scores = json.loads(sub_json) if sub_json else {}
            rationale = json.loads(fit) if fit else {}
        except (json.JSONDecodeError, TypeError):
            continue
        assessment = JobAssessment(
            sub_scores=sub_scores,
            classification="",  # ignored — derived at persist time
            rationale=rationale if isinstance(rationale, dict) else {},
            provider=provider,
        )
        persist_job_assessment(conn, d.dedup_key, assessment, config=config)
    return len(divergences)


def _print_audit(divergences: list[Divergence]) -> None:
    print(f"redrive_classification: {len(divergences)} divergent row(s)")
    for d in divergences[:5]:
        print(f"  {d.dedup_key}: stored={d.stored!r} -> computed={d.computed!r}")


def _resolve_db_path(cli_db: str | None) -> str:
    import os

    if cli_db:
        return cli_db
    env_path = os.environ.get("JOB_CANNON_DB")
    if env_path:
        return env_path
    from job_finder.web import user_data_dirs

    return str(user_data_dirs.db_path())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile stored classification with the rule.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="dry run; report divergences only")
    mode.add_argument("--remediate", action="store_true", help="rewrite divergent rows (default)")
    mode.add_argument("--verify", action="store_true", help="exit 0 iff zero divergence")
    parser.add_argument("--db", default=None, help="path to jobs.db (default: user data dir)")
    parser.add_argument("--config", default=None, help="path to config.yaml (optional)")
    args = parser.parse_args(argv)

    from job_finder.config import load_config

    config = load_config(args.config) if args.config else load_config()
    db_path = _resolve_db_path(args.db)

    conn = sqlite3.connect(db_path)
    try:
        if args.audit:
            _print_audit(find_divergences(conn, config))
            return 0
        if args.verify:
            divergences = find_divergences(conn, config)
            _print_audit(divergences)
            return 0 if not divergences else 1
        # default: remediate
        n = remediate(conn, config)
        print(f"redrive_classification: remediated {n} row(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
