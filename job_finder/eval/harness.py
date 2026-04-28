"""Eval harness orchestration (Phase 5).

Loads the gold set, runs a named variant N times per row, aggregates
metrics, persists the run to ``eval_runs``, and writes a markdown report
to ``.planning/eval_results/``.

Three operational modes (controlled at the CLI by which flags are passed):
    diagnose   — single variant, no baseline (one harness run, one report)
    A/B        — variant + baseline_run_id (paired comparison)
    regression — re-run "baseline" against the gold set; uses the previous
                 baseline run as comparator if the caller supplies one

Per spec D-5.4 every run is persisted, so a baseline does not have to be
"the most recent run." Phase 4 variants will pick a frozen run_id as the
comparator and stick with it across iterations.

No-signal handling
------------------
Gold rows can carry a ``gold_no_signal_axes`` JSON array (Migration 44)
listing axes the labeler explicitly tagged as "couldn't tell". Per the
migration comment, those (axis, row) pairs are dropped from per-axis
MAE / bias / ICC / kappa to avoid charging the model error for axes the
gold itself disclaims signal on. The dropped pairs are still surfaced in
the metrics output via the ``per_axis[axis]['n_used']`` counter so the
report can show how many rows participated in each axis.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from job_finder.eval import metrics

logger = logging.getLogger(__name__)


# Bumped manually if the gold-set sampling/schema changes — readers that
# encounter a metrics row at an old version know the comparison is stale.
GOLD_SET_VERSION = "v1-40-jobs"

AXES: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)

CLASSES: tuple[str, ...] = ("apply", "consider", "skip", "reject", "low_signal")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_gold_rows(db_path: str) -> list[dict]:
    """Read every job row with a non-null gold_classification.

    Returns dicts (not Row objects) so downstream code can mutate without
    having to copy. Sorted by dedup_key so per-job ordering is stable across
    runs (the report's per-job table reads in this order).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT dedup_key, title, company, location, jd_full, sources,
                   classification, sub_scores_json, fit_analysis,
                   gold_classification, gold_sub_scores_json, gold_notes,
                   gold_no_signal_axes, enrichment_tier, legitimacy_note,
                   salary_min, salary_max
            FROM jobs
            WHERE gold_classification IS NOT NULL
            ORDER BY dedup_key
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _load_variant(variant_name: str):
    """Return the prompt module for a named variant.

    'baseline' aliases the production v3_scoring_prompt module. Any other
    name is resolved as ``job_finder.web.scoring_prompts.variants.<name>``.
    The variants subpackage is created lazily by Phase 4 when its first
    candidate prompt is written; Phase 5 only needs 'baseline'.
    """
    if variant_name == "baseline":
        from job_finder.web.scoring_prompts import v3_scoring_prompt as mod

        return mod
    import importlib

    return importlib.import_module(f"job_finder.web.scoring_prompts.variants.{variant_name}")


# ---------------------------------------------------------------------------
# Scoring boundary (test injection point)
# ---------------------------------------------------------------------------


def _score_one(job, conn, config, candidate_context):
    """Thin wrapper around ``score_job`` so tests can monkeypatch this name.

    The harness module is the right injection seam — patching the upstream
    ``score_job`` directly is fragile because callers may import it under
    different names. Patch ``job_finder.eval.harness._score_one`` instead.
    """
    from job_finder.web.job_scorer import score_job

    return score_job(job, conn, config, candidate_context=candidate_context)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _candidate_sub_scores(result) -> dict:
    """Pull sub_scores from a ScoringResult; empty dict on non-ok / no data."""
    if getattr(result, "status", None) != "ok" or result.data is None:
        return {}
    return dict(result.data.sub_scores)


def _no_signal_axes(row: dict) -> set[str]:
    """Parse the gold_no_signal_axes JSON array into a set; tolerate missing/bad data."""
    raw = row.get("gold_no_signal_axes")
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {a for a in parsed if isinstance(a, str)}


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def _compute_metrics(
    gold_rows: list[dict],
    per_job_mean: dict[str, dict[str, float]],
    per_job_runs: dict[str, list[dict]],
) -> dict:
    """Compute the full metric vector from gold + predicted aggregates.

    Per-axis metrics drop (axis, row) pairs the labeler tagged as
    no-signal (gold_no_signal_axes). The per-axis 'n_used' counter
    surfaces how many rows actually contributed.

    Classification metrics use derive_classification on the rounded
    per-job mean sub-scores so the harness measures the same final
    label users would see, not the raw rubric outputs.
    """
    from job_finder.db import derive_classification

    out: dict = {
        "per_axis": {},
        "classification": {},
        "coherence": {},
        "run_level": {},
    }

    # --- Per-axis (drop no-signal axes per row) ---
    for axis in AXES:
        gold_vals: list[int] = []
        pred_vals: list[int] = []
        for r in gold_rows:
            no_signal = _no_signal_axes(r)
            if axis in no_signal:
                continue
            try:
                gold_sub = json.loads(r["gold_sub_scores_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if axis not in gold_sub:
                continue
            mean = per_job_mean.get(r["dedup_key"], {}).get(axis)
            if mean is None or (isinstance(mean, float) and mean != mean):  # NaN
                continue
            gold_vals.append(int(gold_sub[axis]))
            pred_vals.append(round(mean))

        out["per_axis"][axis] = {
            "mae": metrics.mae(gold_vals, pred_vals) if gold_vals else float("nan"),
            "bias": metrics.bias(gold_vals, pred_vals) if gold_vals else float("nan"),
            "icc": metrics.icc([gold_vals, pred_vals]) if gold_vals else float("nan"),
            "qw_kappa": metrics.qw_kappa(gold_vals, pred_vals) if gold_vals else float("nan"),
            "n_used": len(gold_vals),
        }

    # --- Classification: derive from rounded per-job means (matches user-facing label) ---
    pred_cls: list[str] = []
    gold_cls: list[str] = []
    for r in gold_rows:
        sub_round: dict[str, int] = {}
        valid_axes = True
        for a in AXES:
            mean = per_job_mean.get(r["dedup_key"], {}).get(a)
            if mean is None or (isinstance(mean, float) and mean != mean):
                valid_axes = False
                break
            sub_round[a] = round(mean)
        if not valid_axes:
            # All-fail row: predict 'reject' as a safe sentinel so the metric
            # doesn't silently shrink. The error rate is also surfaced via
            # run_level.failed_calls so it's not invisible.
            pred_cls.append("reject")
            gold_cls.append(r["gold_classification"])
            continue
        cls = derive_classification(
            sub_round,
            legitimacy_note=r.get("legitimacy_note"),
            enrichment_tier=r.get("enrichment_tier"),
            jd_full_length=len(r.get("jd_full") or ""),
            low_signal_threshold=1500,
        )
        pred_cls.append(cls)
        gold_cls.append(r["gold_classification"])

    out["classification"] = {
        "per_class": metrics.classification_metrics(gold_cls, pred_cls, CLASSES),
        "confusion_matrix": metrics.confusion_matrix(gold_cls, pred_cls, CLASSES),
        "apply_false_positive_rate": metrics.apply_false_positive_rate(gold_cls, pred_cls),
    }

    # --- Coherence (rationale ↔ score consistency) ---
    coherence_input: list[dict] = []
    for r in gold_rows:
        rounded_sub: dict[str, int] = {}
        for a in AXES:
            mean = per_job_mean.get(r["dedup_key"], {}).get(a)
            # Skip NaN means (all runs failed for that row); the missing axis
            # falls out of coherence checks for this row, which is exactly
            # what we want — we cannot judge consistency without a score.
            if mean is None or (isinstance(mean, float) and mean != mean):
                continue
            rounded_sub[a] = round(mean)
        # gaps_text: read from the most recent rationale payload across all
        # runs (per_job_runs); the first run with a non-empty list wins.
        gaps_text = ""
        for run_entry in per_job_runs.get(r["dedup_key"], []):
            gaps = run_entry.get("gaps") or []
            if gaps:
                gaps_text = " ".join(str(g) for g in gaps)
                break
        coherence_input.append({"sub_scores": rounded_sub, "gaps_text": gaps_text})
    violations = metrics.coherence_violations(coherence_input)
    out["coherence"] = {
        "violations": violations,
        "rate": len(violations) / len(coherence_input) if coherence_input else 0.0,
    }

    # --- Run-level health ---
    n_total = sum(len(runs) for runs in per_job_runs.values())
    n_failed = sum(1 for runs in per_job_runs.values() for r in runs if r.get("status") != "ok")
    out["run_level"] = {
        "total_calls": n_total,
        "failed_calls": n_failed,
        "schema_adherence": (n_total - n_failed) / n_total if n_total else 0.0,
    }

    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    db_path: str,
    variant_name: str = "baseline",
    n_runs: int = 3,
    baseline_run_id: str | None = None,
    report_dir: str = ".planning/eval_results",
    config: dict | None = None,
) -> str:
    """Run a variant against the gold set; persist the run; return the report path.

    Side effects:
        - Inserts one row into eval_runs with the metrics + raw per-job runs.
        - Writes one markdown file to report_dir.
        - Calls the live scoring stack via score_job (unless _score_one is
          monkeypatched in tests).
    """
    config = dict(config or {})
    Path(report_dir).mkdir(parents=True, exist_ok=True)

    gold_rows = _load_gold_rows(db_path)
    # Variant must be importable; failing fast here is preferable to
    # silently using baseline.
    _load_variant(variant_name)

    # Phase 4: inject variant into the config dict so score_job's
    # _build_system_prompt and _resolve_schema pick it up. The CLI flag
    # is the source of truth at eval time and takes precedence over any
    # value already in config.yaml.
    scoring_cfg = dict(config.get("scoring") or {})
    scoring_cfg["prompt_variant"] = variant_name
    config["scoring"] = scoring_cfg

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row

        from job_finder.web.scoring_orchestrator import (
            build_candidate_context,
            load_scoring_profile,
        )

        profile = load_scoring_profile(config)
        candidate_context = build_candidate_context(config, profile)

        per_job_runs: dict[str, list[dict]] = defaultdict(list)
        for row in gold_rows:
            for run_idx in range(n_runs):
                try:
                    result = _score_one(row, conn, config, candidate_context)
                except Exception as exc:
                    logger.exception(
                        "harness: scorer raised for dedup_key=%s run=%d",
                        row.get("dedup_key"),
                        run_idx,
                    )
                    per_job_runs[row["dedup_key"]].append(
                        {
                            "sub_scores": {},
                            "provider": None,
                            "status": "error",
                            "error": str(exc),
                            "gaps": [],
                        }
                    )
                    continue
                gaps: list = []
                if (
                    getattr(result, "status", None) == "ok"
                    and result.data is not None
                    and isinstance(getattr(result.data, "rationale", None), dict)
                ):
                    gaps = list(result.data.rationale.get("gaps") or [])
                per_job_runs[row["dedup_key"]].append(
                    {
                        "sub_scores": _candidate_sub_scores(result),
                        "provider": getattr(result, "provider", None),
                        "status": getattr(result, "status", None),
                        "error": getattr(result, "error", None),
                        "gaps": gaps,
                    }
                )

        per_job_mean: dict[str, dict[str, float]] = {}
        for key, runs in per_job_runs.items():
            valid = [r["sub_scores"] for r in runs if r["sub_scores"]]
            if not valid:
                per_job_mean[key] = {a: float("nan") for a in AXES}
                continue
            per_job_mean[key] = {a: sum(s.get(a, 0) for s in valid) / len(valid) for a in AXES}

        metrics_out = _compute_metrics(gold_rows, per_job_mean, per_job_runs)

        run_id = uuid.uuid4().hex
        timestamp = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO eval_runs
                (run_id, timestamp, variant_name, baseline_run_id, gold_set_version,
                 n_runs, config_json, metrics_json, per_job_json, report_path, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                timestamp,
                variant_name,
                baseline_run_id,
                GOLD_SET_VERSION,
                n_runs,
                json.dumps(config, default=str),
                json.dumps(metrics_out),
                json.dumps(per_job_runs),
                "",
                None,
            ),
        )
        conn.commit()

        from job_finder.eval.report import write_report

        report_path = write_report(
            run_id=run_id,
            variant_name=variant_name,
            baseline_run_id=baseline_run_id,
            gold_rows=gold_rows,
            per_job_mean=per_job_mean,
            per_job_runs=per_job_runs,
            metrics_out=metrics_out,
            report_dir=report_dir,
            db_path=db_path,
        )
        conn.execute(
            "UPDATE eval_runs SET report_path=? WHERE run_id=?",
            (report_path, run_id),
        )
        conn.commit()
        return report_path
    finally:
        conn.close()


__all__ = ["AXES", "CLASSES", "GOLD_SET_VERSION", "run"]
