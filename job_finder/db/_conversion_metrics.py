"""Read-only conversion-signal analytics: application- and callback-rate by fit band.

Zero model calls, zero writes. Answers 'does a high fit-grade predict that the
owner applied, and that the application converted?' — the validity gauge the
scorer (job_scorer.py) otherwise lacks. Mirrors the read-only sibling detector
pattern (_check_owner_idle / _check_score_rot) and the dashboard read-aggregate
pattern (get_pipeline_summary).
"""

from __future__ import annotations

import sqlite3

from job_finder.constants import CLASSIFICATIONS, PIPELINE_STATUSES

# The positive-progression run: an application that reached >= 'applied'
# advanced. Named explicitly (in canonical order) and guarded against drift
# from the source vocabulary, so a future reorder/rename of PIPELINE_STATUSES
# cannot silently fold 'rejected' into "positive". 'applied' is the denominator
# floor for callback rate; 'phone_screen' is the first *callback* stage.
_PROGRESSION = ("applied", "phone_screen", "technical", "onsite", "offer", "accepted")
# Single-point drift guard: every progression stage must exist in the canonical
# vocabulary, else a rename silently drops it from the denominator/numerator.
assert set(_PROGRESSION) <= set(PIPELINE_STATUSES), (
    "progression stages drifted from PIPELINE_STATUSES"
)
POSITIVE_STAGES: tuple[str, ...] = _PROGRESSION


def compute_conversion_by_band(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per fit-band application- and callback-rate, read-only.

    For each band in CLASSIFICATIONS, over jobs that are SCORED
    (scoring_model IS NOT NULL AND classification IS NOT NULL):

      scored          -- count of scored jobs in the band
      applied         -- count that EVER reached >= 'applied'
                         (max-stage-ever from pipeline_events, NOT current status)
      converted       -- of the applied ones, count that EVER reached
                         >= 'phone_screen'
      application_rate -- applied / scored          (None if scored == 0)
      callback_rate    -- converted / applied       (None if applied == 0)

    'applied' and 'converted' are computed from the FURTHEST stage each job
    ever reached in pipeline_events.to_status, so a job that went
    applied -> phone_screen -> rejected counts as applied AND converted even
    though its current jobs.pipeline_status is 'rejected'.

    The callback_rate denominator is the APPLIED count, never the scored count,
    so an unapplied high-fit job cannot deflate the rate.

    Returns a dict keyed by band (every band in CLASSIFICATIONS present, even
    at zero) -> the per-band dict above. Pure read; commits nothing.
    """
    # Save and restore row_factory to avoid side effects
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row

    try:
        # Build CASE mapping programmatically from POSITIVE_STAGES
        case_clauses = []
        for rank, stage in enumerate(POSITIVE_STAGES, start=1):
            case_clauses.append(f"WHEN '{stage}' THEN {rank}")
        case_expr = "CASE pe.to_status " + " ".join(case_clauses) + " ELSE 0 END"

        # Compute max-stage-ever per job from pipeline_events
        max_stage_query = f"""
            SELECT job_id, MAX(rank) AS max_rank
            FROM (
                SELECT pe.job_id,
                       {case_expr} AS rank
                FROM pipeline_events pe
            ) ranked
            GROUP BY job_id
        """

        # Seed result with all bands (even empty ones)
        result: dict[str, dict] = {}
        for band in CLASSIFICATIONS:
            result[band] = {
                "scored": 0,
                "applied": 0,
                "converted": 0,
                "application_rate": None,
                "callback_rate": None,
            }

        # Get scored jobs by band
        scored_rows = conn.execute(
            "SELECT classification, COUNT(*) as cnt FROM jobs "
            "WHERE scoring_model IS NOT NULL AND classification IS NOT NULL "
            "GROUP BY classification"
        ).fetchall()

        for row in scored_rows:
            band = row["classification"]
            if band in result:
                result[band]["scored"] = row["cnt"]

        # Get max-stage-ever for all jobs with pipeline events
        max_stage_rows = conn.execute(max_stage_query).fetchall()
        job_max_stage = {row["job_id"]: row["max_rank"] for row in max_stage_rows}

        # Get classification for all SCORED jobs (to map job_id -> band)
        # Only scored jobs (scoring_model IS NOT NULL) should be counted
        job_classification = {}
        class_rows = conn.execute(
            "SELECT dedup_key, classification FROM jobs "
            "WHERE classification IS NOT NULL AND scoring_model IS NOT NULL"
        ).fetchall()
        for row in class_rows:
            job_classification[row["dedup_key"]] = row["classification"]

        # Count applied (max_rank >= 1) and converted (max_rank >= 2) per band
        for job_id, max_rank in job_max_stage.items():
            band = job_classification.get(job_id)
            if band and band in result:
                if max_rank >= 1:  # reached 'applied' or higher
                    result[band]["applied"] += 1
                if max_rank >= 2:  # reached 'phone_screen' or higher
                    result[band]["converted"] += 1

        # Compute rates
        for band in result:
            scored = result[band]["scored"]
            applied = result[band]["applied"]
            converted = result[band]["converted"]

            if scored > 0:
                result[band]["application_rate"] = applied / scored
            else:
                result[band]["application_rate"] = None

            if applied > 0:
                result[band]["callback_rate"] = converted / applied
            else:
                result[band]["callback_rate"] = None

        return result
    finally:
        conn.row_factory = original_row_factory
