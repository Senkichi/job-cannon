"""Heavy job-runner functions used by the scheduler's register helpers.

Two functions live here because their bodies bloated ``_jobs.py`` past
the per-file size target:

  - ``run_enrichment_backfill_two_stage`` -- the two-stage post-ingestion
    enrichment + scoring job (called from the ``enrichment_backfill``
    register helper at 1, 9, 17 cron).
  - ``run_health_check`` -- the daily 6:00 AM heartbeat that asserts key
    subsystems ran recently (called from the ``health_heartbeat`` register
    helper).

Both are pure functions of their inputs (no module-level state). Tests
that exercise the scheduler at registration level still patch
``BackgroundScheduler`` / ``_jobs.CronTrigger``; tests that exercise
the runner bodies (none currently) would patch this module directly.
"""

import logging
from typing import Any

from job_finder.secrets import get_secret

logger = logging.getLogger(__name__)


def run_enrichment_backfill_two_stage(
    db_path: str, config: dict, *, run_id: str | None = None
) -> dict[str, Any]:
    """Run the post-ingestion enrichment backfill, score new rows, then drain
    the empty-location backlog via a cheap extraction-only pass.

    Stage 1: fill ``jd_full`` via the cost-ordered tier pipeline.
    Stage 2: score every newly-enriched row.
    Stage 3: extraction-only location pass — drains rows that already have a
      good jd_full but an empty location, including terminal-tier rows that
      the regular cascade skips. Capped at 50 rows/run so the nightly job
      does not block long on a large backlog (50/run × 3 runs/day ≈ 2–3 days
      to drain the ~325-row careers-crawl backlog). D-5 / #388.

    Without stage 2 the v3.0 multi-stage pipeline leaks: ingestion-time
    scoring sees empty ``jd_full`` and skips, and nothing else picks
    the row up.

    Returns a metrics dict consumed by the scheduler's tracked-job
    extract_metadata callable.

    ``run_id`` (issue #215): the run-envelope correlation id from the
    scheduler/harness wrapper. Threaded into ``run_scoring`` so each
    per-job ``score`` event the orchestrator emits onto
    ``run_events.jsonl`` carries the same id as this run's
    ``run_start`` / ``run_end`` envelope.
    """
    from job_finder.web.data_enricher import (
        run_enrichment_backfill,
        run_location_extraction_backfill,
    )
    from job_finder.web.db_helpers import standalone_connection
    from job_finder.web.scoring_runner import run_scoring

    result: dict[str, Any] = {
        "enriched": 0,
        "location_resolved": 0,
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "errors": [],
    }

    # Stage 1: enrichment
    try:
        serpapi_key = get_secret("sources.serpapi.api_key", config=config)
        enriched = run_enrichment_backfill(
            db_path,
            serpapi_key=serpapi_key,
            config=config,
            limit=None,
        )
        result["enriched"] = enriched if isinstance(enriched, int) else 0
        logger.info("Enrichment backfill: %s", result["enriched"])
    except Exception as e:
        logger.error("Enrichment backfill failed: %s", e)
        result["errors"].append(f"enrichment: {type(e).__name__}: {e}")
        return result

    # Stage 2: post-enrichment scoring
    try:
        with standalone_connection(db_path) as score_conn:
            rows = score_conn.execute(
                "SELECT dedup_key FROM jobs "
                "WHERE jd_full IS NOT NULL AND jd_full != '' "
                "AND classification IS NULL "
                "AND (pipeline_status IS NULL "
                "     OR pipeline_status NOT IN ('archived', 'dismissed'))"
            ).fetchall()
        dedup_keys = [r[0] for r in rows]
        if not dedup_keys:
            logger.info("Post-enrichment scoring: nothing to score")
        else:
            summary = run_scoring(dedup_keys, config, db_path, run_id=run_id)
            result["scored"] = summary.get("scored", 0)
            result["classified_apply"] = summary.get("classified_apply", 0)
            result["classified_consider"] = summary.get("classified_consider", 0)
            result["classified_skip"] = summary.get("classified_skip", 0)
            result["classified_reject"] = summary.get("classified_reject", 0)
            logger.info("Post-enrichment scoring: %s", summary)
    except Exception as e:
        logger.error("Post-enrichment scoring failed: %s", e)
        result["errors"].append(f"post_scoring: {type(e).__name__}: {e}")

    # Stage 3: extraction-only location pass (D-5, #388).
    # Runs after scoring so a row that just got jd_full in stage 1 can be
    # caught here on the same run rather than waiting for the next cycle.
    # Capped at 50/run to stay lightweight; best-effort — never aborts the
    # result dict even on total failure.
    try:
        location_resolved = run_location_extraction_backfill(
            db_path,
            config=config,
            limit=50,
        )
        result["location_resolved"] = location_resolved
        logger.info("Location extraction backfill: %d resolved", location_resolved)
    except Exception as e:
        logger.warning("Location extraction backfill failed: %s", e)
        result["errors"].append(f"location_extract: {type(e).__name__}: {e}")

    return result


def _derive_degraded_keys(issues: list[str], degraded_sources: list[str]) -> set[str]:
    """Map the heartbeat's free-text ``issues`` + degraded sources to stable keys.

    The verdict strings are generated in ``run_health_check`` itself, so prefix
    matching against the known signal shapes is stable. Returns a *new* set
    (no mutation of inputs):

      - ``ingestion``  <- "No ingestion in last Xh" (X is derived from cadence_preset)
      - ``staleness``  <- "Stale detection missed last night"
      - ``oauth``      <- "OAuth token invalid: ..."
      - ``db``         <- "Health check DB error: ..."
      - ``owner_idle`` <- "Owner idle: ..."
      - ``score_rot``  <- "Score rot: ..."
      - ``cost_health`` <- "Cost health: ..." (issue #581)
      - ``funnel_unexplained`` <- "Funnel unexplained: ..." (issue #587)
      - ``concentration`` <- "Concentration: ..." (issue #592)
      - ``conversion_signal`` <- "Conversion signal degraded: ..." (issue #597)
      - ``coverage`` <- "Source deadman: ..." (issue #588)
      - ``source:<name>`` <- signal-3 "<action>: <n> failures in 24h" rows and
        every ``source_health.status='degraded'`` source (C2-4).
    """
    keys: set[str] = set()
    for issue in issues:
        if issue.startswith("No ingestion"):
            keys.add("ingestion")
        elif issue.startswith("Owner idle"):
            keys.add("owner_idle")
        elif issue.startswith("Score rot"):
            keys.add("score_rot")
        elif issue.startswith("Cost health"):
            keys.add("cost_health")
        elif issue.startswith("Funnel unexplained"):
            keys.add("funnel_unexplained")
        elif issue.startswith("Concentration"):
            keys.add("concentration")
        elif issue.startswith("Conversion signal degraded"):
            keys.add("conversion_signal")
        elif issue.startswith("Source deadman"):
            keys.add("coverage")
        elif issue.startswith("Stale detection missed"):
            keys.add("staleness")
        elif issue.startswith("OAuth token invalid"):
            keys.add("oauth")
        elif issue.startswith("Health check DB error"):
            keys.add("db")
        else:
            # signal 3: "<action>: <cnt> failures in 24h"
            action = issue.split(":", 1)[0].strip()
            if action:
                keys.add(f"source:{action}")
    for source in degraded_sources:
        keys.add(f"source:{source}")
    return keys


def _fire_escalation(escalated: list[dict], issues: list[str], config: dict) -> None:
    """Best-effort egress for sustained-degradation escalations (C2-5, #438/#440).

    Lazily imports the notification façade so this call site lands independently
    of the egress transport; an unavailable egress logs and returns. Never
    raises — a dead egress must not break the heartbeat's best-effort contract.

    ``escalated`` is a list of ``{"signal_key", "consecutive_degraded"}`` dicts.
    """
    try:
        from job_finder.web.notifications import notify
    except Exception:
        logger.warning("escalation egress unavailable (notifications import failed)")
        return

    keys = ", ".join(e["signal_key"] for e in escalated)
    title = f"Job Cannon: sustained health degradation ({keys})"
    streaks = "; ".join(
        f"{e['signal_key']} ({e['consecutive_degraded']} consecutive checks)" for e in escalated
    )
    body = "Signals degraded past the escalation threshold: " + streaks
    if issues:
        body += "\n\nIssues:\n" + "\n".join(f"- {i}" for i in issues)

    # Hire-day handoff: the owner-idle signal is the one death mode whose most
    # likely cause is *good* news (landed a role and drifted away). When it
    # escalates, the terse metric isn't enough -- tell the owner what to do so a
    # silent museum-piece decline isn't the default. Only appended for owner_idle.
    if any(e["signal_key"] == "owner_idle" for e in escalated):
        body += (
            "\n\n---\n"
            "About that owner-idle signal: if you've landed a role, congratulations -- "
            "you can wind Job Cannon down anytime with `job-cannon stop` (it halts the app "
            "and disables the keepalive supervisor). If you're still searching and just "
            "stepped away, the board has kept scanning and scoring on schedule, so your "
            "queue is waiting whenever you're ready. Nothing is required here; this note "
            "only means Job Cannon has been running unattended for a while."
        )

    try:
        notify(title, body, severity="critical", config=config)
    except Exception:
        logger.exception("escalation egress failed")


def _escalate_degradation(
    db_path: str,
    issues: list[str],
    degraded_sources: list[str],
    config: dict,
    now_iso: str,
) -> None:
    """Track per-signal consecutive-degraded streaks and fire egress at threshold.

    Read-modify-write against ``health_escalation_state`` (m105): increment each
    currently-degraded key, reset recovered keys to 0, and fire ``_fire_escalation``
    once per streak when a key first reaches ``health.escalation_consecutive_threshold``
    (default 3). The fire-once gate is ``last_escalated_at IS NULL`` — stamped on
    fire, cleared on recovery so a fresh streak can escalate again.

    Best-effort: callers wrap this so it never breaks the heartbeat, but it is
    also self-contained against DB errors.
    """
    from job_finder.web.db_helpers import standalone_connection as _sc

    threshold = int((config.get("health", {}) or {}).get("escalation_consecutive_threshold", 3))
    degraded_keys = _derive_degraded_keys(issues, degraded_sources)

    with _sc(db_path) as conn:
        existing = {
            row["signal_key"]: row
            for row in conn.execute(
                "SELECT signal_key, consecutive_degraded, last_escalated_at "
                "FROM health_escalation_state"
            ).fetchall()
        }

        escalated: list[dict] = []
        for key in sorted(degraded_keys):
            prev = existing.get(key)
            prev_count = prev["consecutive_degraded"] if prev else 0
            prev_escalated_at = prev["last_escalated_at"] if prev else None
            new_count = prev_count + 1

            should_fire = new_count >= threshold and prev_escalated_at is None
            new_escalated_at = now_iso if should_fire else prev_escalated_at

            conn.execute(
                "INSERT INTO health_escalation_state "
                "(signal_key, consecutive_degraded, last_status, last_escalated_at, updated_at) "
                "VALUES (?, ?, 'degraded', ?, ?) "
                "ON CONFLICT(signal_key) DO UPDATE SET "
                "consecutive_degraded = excluded.consecutive_degraded, "
                "last_status = excluded.last_status, "
                "last_escalated_at = excluded.last_escalated_at, "
                "updated_at = excluded.updated_at",
                (key, new_count, new_escalated_at, now_iso),
            )
            if should_fire:
                escalated.append({"signal_key": key, "consecutive_degraded": new_count})

        # Recovered keys: reset counter and clear the fire-once gate so a future
        # degradation streak can escalate again.
        for key in existing:
            if key not in degraded_keys:
                conn.execute(
                    "UPDATE health_escalation_state SET "
                    "consecutive_degraded = 0, last_status = 'healthy', "
                    "last_escalated_at = NULL, updated_at = ? "
                    "WHERE signal_key = ?",
                    (now_iso, key),
                )
        conn.commit()

    if escalated:
        _fire_escalation(escalated, issues, config)


def _check_owner_idle(conn, config: dict) -> str | None:
    """Owner-idle alarm -- the pre-mortem's #1 death mode made visible.

    The tool's heartbeat is the owner's own attention; if they drift away (e.g.
    after landing a job) the scheduler keeps firing while rot accrues unwatched.
    This fires when the most recent *human* action (``HUMAN_ACTIONS`` -- a person
    touching the UI, not a scheduled row) is older than ``health.owner_idle_days``
    (default 14; ``<= 0`` disables). Read-only; returns an issue string or None.

    Scope note: this only runs while the app/scheduler is alive (it IS a
    scheduled job), so it catches "running unattended", not "switched off
    entirely" -- the latter is the external-deadman's job. A brand-new install
    with no human action yet is not alarmed (that is the adoption-void path).
    Passive board-viewing is not instrumented, so "idle" means "no interaction";
    the generous default keeps that from nagging an engaged-but-quiet owner.
    """
    from job_finder.web.activity_tracker import HUMAN_ACTIONS

    idle_days = int((config.get("health", {}) or {}).get("owner_idle_days", 14))
    if idle_days <= 0:
        return None

    human = tuple(sorted(HUMAN_ACTIONS))
    placeholders = ",".join("?" * len(human))
    row = conn.execute(
        f"SELECT MAX(occurred_at) FROM user_activity WHERE action IN ({placeholders})",
        human,
    ).fetchone()
    last_human = row[0] if row else None
    if not last_human:
        return None

    days = conn.execute(
        "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)", (last_human,)
    ).fetchone()[0]
    if days is None or days < idle_days:
        return None
    return f"Owner idle: no human activity in {days}d (threshold {idle_days}d)"


def _check_score_rot(conn, config: dict) -> str | None:
    """Score-rot parity alarm -- the pre-mortem's #3 death mode made visible.

    Re-runs ``derive_classification`` over each LLM-scored row's ALREADY-STORED
    sub-scores under today's thresholds (ZERO model calls) and counts rows whose
    verdict would now differ -- the seam between scoring eras that, unlike
    titles/dates/JDs, has no self-healing re-sweep. Fires when the drift fraction
    reaches ``health.score_rot_fraction`` (default 0.01; ``> 1`` disables).
    Read-only; returns an issue string or None.

    ``low_signal`` rows are excluded from the count: that verdict can stem from a
    per-run ``degenerate`` (all-providers-uniform) signal that is NOT persisted
    and so cannot be faithfully reconstructed -- including them would surface
    phantom drift. Rows with unparseable sub-scores are skipped, never raised.
    """
    import json

    from job_finder.db._classification import derive_classification

    scoring = config.get("scoring", {}) or {}
    low_signal_threshold = int(scoring.get("low_signal_jd_chars", 1500))
    apply_mean_floor = float(scoring.get("apply_mean_floor", 3.5))
    apply_min_strong_axes = int(scoring.get("apply_min_strong_axes", 3))
    floor = float((config.get("health", {}) or {}).get("score_rot_fraction", 0.01))
    if floor > 1.0:
        return None

    rows = conn.execute(
        "SELECT classification, sub_scores_json, legitimacy_note, enrichment_tier, "
        "COALESCE(LENGTH(jd_full), 0) "
        "FROM jobs "
        "WHERE scoring_model IS NOT NULL AND sub_scores_json IS NOT NULL "
        "AND classification IS NOT NULL AND classification != 'low_signal'"
    ).fetchall()

    audited = 0
    drift = 0
    for stored, sub_scores_json, legitimacy_note, enrichment_tier, jd_len in rows:
        try:
            sub_scores = json.loads(sub_scores_json)
            if not isinstance(sub_scores, dict) or not sub_scores:
                continue
            rederived = derive_classification(
                sub_scores=sub_scores,
                legitimacy_note=legitimacy_note,
                enrichment_tier=enrichment_tier,
                jd_full_length=jd_len,
                low_signal_threshold=low_signal_threshold,
                apply_mean_floor=apply_mean_floor,
                apply_min_strong_axes=apply_min_strong_axes,
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        audited += 1
        if rederived != stored:
            drift += 1

    if audited == 0 or drift == 0:
        return None
    frac = drift / audited
    if frac < floor:
        return None

    # Stratigraphy: per-model breakdown so the operator can see which scoring era is rotting
    model_rows = conn.execute(
        "SELECT scoring_model, COUNT(*) FROM jobs WHERE scoring_model IS NOT NULL GROUP BY scoring_model ORDER BY COUNT(*) DESC"
    ).fetchall()
    if model_rows:
        model_breakdown = ", ".join(f"{model}={count}" for model, count in model_rows)
        return f"Score rot: {drift}/{audited} stored verdicts ({frac:.1%}) differ from today's rule | by model: {model_breakdown}"
    return f"Score rot: {drift}/{audited} stored verdicts ({frac:.1%}) differ from today's rule"


def _check_funnel_unexplained(conn, config: dict) -> str | None:
    """Funnel unexplained-drop alarm -- issue #587 reconciliation identity.

    Reads the most recent ingestion run's persisted funnel metadata and checks
    whether the unexplained drop count exceeds ``health.funnel_unexplained_max``
    (default 0; ``< 0`` disables). The unexplained count is the self-auditing
    invariant: jobs_in must equal jobs_passed + sum(drop_buckets) + jobs_errored.
    Any non-zero value indicates a silent-drop bug (a code path that discards a
    row without incrementing a counter). Read-only; returns an issue string or None.
    """
    import json

    max_unexplained = int((config.get("health", {}) or {}).get("funnel_unexplained_max", 0))
    if max_unexplained < 0:
        return None

    # Read the most recent ingestion run with funnel metadata
    row = conn.execute(
        "SELECT metadata FROM runs WHERE source = 'ingestion' AND metadata IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    if not row:
        return None

    try:
        funnel = json.loads(row[0])
        if not isinstance(funnel, dict):
            return None
        unexplained = funnel.get("unexplained", 0)
        if unexplained is None or not isinstance(unexplained, int):
            return None
        if unexplained > max_unexplained:
            return f"Funnel unexplained: {unexplained} rows in last ingestion run"
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

    return None


def _check_concentration(conn, config: dict) -> str | None:
    """Concentration alarm -- surfaced fit-floor cohort diversity erosion (issue #592).

    Computes normalized HHI for surfaced jobs (apply/consider classifications) grouped
    by employer and by ATS platform. Fires when either grouping's normalized HHI
    exceeds ``health.surfaced_concentration_ceiling`` (default 0.60; ``> 1`` disables)
    AND the total surfaced jobs is at least ``health.surfaced_concentration_min_jobs``
    (default 25) to avoid false alarms on cold-start boards. Read-only; returns an
    issue string or None.
    """
    from job_finder.db._dashboard_queries import get_surfaced_concentration

    ceiling = float((config.get("health", {}) or {}).get("surfaced_concentration_ceiling", 0.60))
    min_jobs = int((config.get("health", {}) or {}).get("surfaced_concentration_min_jobs", 25))

    if ceiling > 1.0:
        return None

    concentration = get_surfaced_concentration(conn)

    # Check employer grouping
    employer = concentration["by_employer"]
    if (
        employer["total"] >= min_jobs
        and employer["hhi"] is not None
        and employer["hhi"] >= ceiling
    ):
        return (
            f"Concentration: employer HHI {employer['hhi']:.2f} over {employer['total']} "
            f"surfaced jobs (ceiling {ceiling:.2f})"
        )

    # Check platform grouping
    platform = concentration["by_platform"]
    if (
        platform["total"] >= min_jobs
        and platform["hhi"] is not None
        and platform["hhi"] >= ceiling
    ):
        return (
            f"Concentration: platform HHI {platform['hhi']:.2f} over {platform['total']} "
            f"surfaced jobs (ceiling {ceiling:.2f})"
        )

    return None


def _check_cost_health(conn, config: dict) -> str | None:
    """Cost-ledger free/paid health watch -- Detector C.

    Groups the scoring_costs ledger by provider over a trailing N-day window and
    flags two regressions of the free-first AI provider cascade:
      1. Paid inference detected: any paid-provider row appears (surprise spend).
      2. Free providers absent: zero free-provider rows despite scoring activity
         (broken free rung).

    The window is controlled by health.cost_health_window_days (default 7; <= 0
    disables). The 'free providers absent' arm is gated by
    health.cost_health_min_activity (default 1) to avoid alarming a fresh/idle
    install with no scoring history. Paid-leak detection is activity-independent.

    scoring_costs is NOT an LLM-only ledger -- non-inference quota writers also
    append rows (serpapi_enrichment via data_enricher._record_serpapi_call and
    google_cse search-quota rows, both cost_usd=0). The detector scopes to genuine
    LLM inference so a $0 quota row is never mis-reported as surprise paid spend,
    and a running search source can never mask a broken LLM free rung.
    Read-only; returns an issue string or None.
    """
    from job_finder.web.claude_client import FREE_PROVIDERS
    from job_finder.web.provider_catalog import SUPPORTED_PROVIDERS

    window_days = int((config.get("health", {}) or {}).get("cost_health_window_days", 7))
    if window_days <= 0:
        return None

    min_activity = int((config.get("health", {}) or {}).get("cost_health_min_activity", 1))

    # Everything derives from the provider_catalog roster (no hand-maintained list):
    # "google_cse" is the one documented non-LLM label carried in FREE_PROVIDERS for
    # budget purposes; every other FREE label and every SUPPORTED_PROVIDERS spec is an
    # inference provider. A paid leak is an inference row from a NON-free spec.
    inference_providers = (SUPPORTED_PROVIDERS | FREE_PROVIDERS) - {"google_cse"}
    paid_ai_providers = SUPPORTED_PROVIDERS - FREE_PROVIDERS

    rows = conn.execute(
        "SELECT provider, COUNT(*) AS calls "
        "FROM scoring_costs "
        "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-' || ? || ' days') "
        "GROUP BY provider",
        (window_days,),
    ).fetchall()

    problems: list[str] = []
    inference_calls = 0
    free_calls = 0

    for provider, calls in rows:
        if provider not in inference_providers:
            continue  # non-LLM quota row (serpapi_enrichment / google_cse search) -- skip
        inference_calls += calls
        if provider in paid_ai_providers:
            # Paid leak: activity-independent (single paid call cracks the $0 invariant)
            problems.append(f"paid inference detected: {provider} ({calls} calls)")
        elif provider in FREE_PROVIDERS:
            free_calls += calls

    # Free rung broke: only alarm when there is real inference activity and none of it
    # was free. The `inference_calls > 0` guard keeps a misconfigured min_activity <= 0
    # from firing on a legitimately idle/empty ledger.
    if inference_calls > 0 and inference_calls >= min_activity and free_calls == 0:
        problems.append(
            f"free providers absent despite {inference_calls} scoring calls in last {window_days}d"
        )

    if not problems:
        return None

    return "Cost health: " + "; ".join(sorted(problems))


def _check_conversion_signal(conn, config: dict) -> str | None:
    """Conversion-signal alarm -- does the fit-grade predict outcomes?

    Pools the raw applied/converted counts (from compute_conversion_by_band)
    across the high-fit bands (apply/consider) and the low-fit bands (skip/reject)
    into ONE volume-weighted callback rate per side, and flags when the high-fit
    rate is NOT higher than the low-fit rate -- i.e. the grade is failing to
    predict real outcomes. Gated on config["health"]["conversion_min_applied"]
    (default 10; <= 0 disables) applied SYMMETRICALLY -- both sides need at least
    that many applications, so a lone low-fit application can't fire a false
    alarm. Read-only; returns an issue string or None.
    """
    from job_finder.db import compute_conversion_by_band

    min_applied = int((config.get("health", {}) or {}).get("conversion_min_applied", 10))
    if min_applied <= 0:
        return None

    try:
        by_band = compute_conversion_by_band(conn)
    except Exception:
        # Defensive: any error in the read-only computation should not break the heartbeat
        return None

    high_fit_bands = ["apply", "consider"]
    low_fit_bands = ["skip", "reject"]

    # Pool the raw counts across each band group and compute ONE volume-weighted
    # (pooled) callback rate per side -- NOT a simple mean of per-band rates. A
    # simple mean lets a tiny band (e.g. consider n=0->0%) swing the verdict with
    # the same weight as a large one; pooling weights each application equally.
    high_fit_applied = sum(by_band[band]["applied"] for band in high_fit_bands)
    high_fit_converted = sum(by_band[band]["converted"] for band in high_fit_bands)
    low_fit_applied = sum(by_band[band]["applied"] for band in low_fit_bands)
    low_fit_converted = sum(by_band[band]["converted"] for band in low_fit_bands)

    # Symmetric sample-size floor: BOTH sides need at least min_applied
    # applications. Flooring only the high-fit side lets a single low-fit
    # application (n=1 -> 100% callback) fire a false "grade doesn't predict"
    # alarm. min_applied <= 0 already short-circuited above, so both denominators
    # are >= 1 here and the pooled rates below never divide by zero.
    if high_fit_applied < min_applied or low_fit_applied < min_applied:
        return None  # Not enough data on one or both sides to be meaningful

    high_fit_rate = high_fit_converted / high_fit_applied
    low_fit_rate = low_fit_converted / low_fit_applied

    # Fire alarm if high-fit callback rate is not higher than low-fit
    if high_fit_rate <= low_fit_rate:
        return (
            f"Conversion signal degraded: high-fit callback rate ({high_fit_rate:.1%}) "
            f"not higher than low-fit ({low_fit_rate:.1%})"
        )

    return None


def _check_source_deadman(conn, config: dict) -> list[str]:
    """Supply-side per-source deadman alarm -- detects silent source failures.

    Checks two ATS supply-side recency classes for age-since-last-success:
      1. ATS scanner fleet: MAX(companies.last_scanned_at) over scannable
         companies (ats_probe_status='hit'). This is the fleet-level "are scanners
         producing" signal -- a single stale company is normal (cohort rotation),
         but a fleet with no recent successful scan is the deadman.
      2. company_scan_log age: MAX(scanned_at) for the ATS-surface deadman input.

    The alarm fires when age > derived_window * tolerance, where:
      - derived_window = expected_ats_scan_window_hours() (24h daily cadence)
      - tolerance = health.source_deadman_tolerance (default 2.0; <= 0 disables)

    Feed ingestion recency is owned by signal #1 in run_health_check (the
    "No ingestion in last Xh" alarm) and is NOT checked here to avoid double-count.

    Returns a list of issue strings (one per dead class) or empty list.
    Read-only; never raises (swallows schema drift errors).
    """
    from job_finder.web.scheduler._schedule import expected_ats_scan_window_hours

    try:
        tolerance = float((config.get("health", {}) or {}).get("source_deadman_tolerance", 2.0))
        if tolerance <= 0:
            return []

        window_hours = expected_ats_scan_window_hours()
        window_seconds = window_hours * 3600
        allowed_age_seconds = window_seconds * tolerance
    except Exception:
        # A malformed tolerance value must not break the heartbeat.
        return []

    issues: list[str] = []

    # Each recency class is guarded independently so schema drift in one
    # (e.g. a missing table) cannot discard a real outage detected by the other.
    # 1. ATS scanner fleet: MAX(last_scanned_at) over scannable companies
    try:
        row = conn.execute(
            "SELECT MAX(last_scanned_at) FROM companies WHERE ats_probe_status = 'hit'"
        ).fetchone()
        if row and row[0]:
            age_seconds = conn.execute(
                "SELECT CAST(strftime('%s', 'now') - strftime('%s', ?) AS INTEGER)", (row[0],)
            ).fetchone()[0]
            if age_seconds is not None and age_seconds > allowed_age_seconds:
                issues.append(
                    f"Source deadman: ATS scanner fleet — no successful scan in "
                    f"{age_seconds / 3600:.1f}h (window {window_hours}h)"
                )
    except Exception:
        pass

    # 2. company_scan_log age: MAX(scanned_at) for ATS-surface deadman
    try:
        row = conn.execute("SELECT MAX(scanned_at) FROM company_scan_log").fetchone()
        if row and row[0]:
            age_seconds = conn.execute(
                "SELECT CAST(strftime('%s', 'now') - strftime('%s', ?) AS INTEGER)",
                (row[0],),
            ).fetchone()[0]
            if age_seconds is not None and age_seconds > allowed_age_seconds:
                issues.append(
                    f"Source deadman: ATS scan log — no scan in "
                    f"{age_seconds / 3600:.1f}h (window {window_hours}h)"
                )
    except Exception:
        pass

    return issues


def run_health_check(app) -> None:
    """Daily health heartbeat -- verify key subsystems ran recently.

    Logs ``HEALTH_OK`` (info) when ingestion + stale detection + OAuth all
    look nominal, otherwise logs ``HEALTH_DEGRADED`` (warning) with a
    semicolon-joined list of issues. Best-effort; never raises.

    Routes the verdict to durable channels: writes one ``scheduled_health``
    row to ``user_activity`` (surfaces in the dashboard "User Activity" table
    via the existing ``meta.status`` branch) and emits a ``run_events``
    ``run_start``/``run_end`` envelope with ``disposition='degraded'`` when
    any issue was detected, ``'completed'`` otherwise. Both writers are
    no-raise, so the heartbeat's best-effort contract holds.
    """
    import time as _time
    from datetime import UTC, datetime

    from job_finder.web import run_events
    from job_finder.web.activity_tracker import ACTION_SCHEDULED_HEALTH, log_activity
    from job_finder.web.db_helpers import get_config_snapshot

    with app.app_context():
        db_path = app.config.get("DB_PATH", "jobs.db")
        issues: list[str] = []
        degraded_sources: list[str] = []
        # Hoisted once and reused by the owner-idle / score-rot checks, the
        # autoheal sweep, and the escalation pass. Guarded so a config read can
        # never break the heartbeat's no-raise contract.
        try:
            config = get_config_snapshot(app)
        except Exception:
            config = {}

        t0 = _time.time()
        run_id = run_events.start(job="health", source="scheduler", db_path=db_path)

        try:
            from job_finder.web.db_helpers import standalone_connection as _sc
            from job_finder.web.scheduler._schedule import (
                expected_ingestion_window_hours,
                expected_staleness_window_hours,
            )

            with _sc(db_path) as conn:
                # 1. Did ingestion run in the last derived window?
                # Window is derived from cadence_preset (default standard = 8h max gap).
                # This is a separate pre-existing alarm and is NOT multiplied by
                # source_deadman_tolerance (that knob only affects the deadman).
                # Uses epoch math to avoid separator bugs (occurred_at is 'T'-separated,
                # datetime('now') is space-separated).
                preset = (config.get("scheduler", {}) or {}).get("cadence_preset", "standard")
                ingestion_window_hours = expected_ingestion_window_hours(preset)
                ingestion_window_seconds = ingestion_window_hours * 3600
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action IN ('scheduled_sync', 'sync') "
                    f"AND CAST(strftime('%s', occurred_at) AS INTEGER) >= CAST(strftime('%s', 'now') - {ingestion_window_seconds} AS INTEGER)"
                ).fetchone()
                if not row[0]:
                    issues.append(f"No ingestion in last {ingestion_window_hours:.0f}h")

                # 2. Did stale detection run last night?
                # Window is derived from expected_staleness_window_hours() (26h = 24h + 2h tolerance)
                # Writer uses ACTION_SCHEDULED_STALENESS = 'scheduled_staleness'
                # (see activity_tracker.py). The legacy 'scheduled_stale_detection'
                # string is no longer emitted by any code path.
                # Uses epoch math to avoid separator bugs.
                staleness_window_hours = expected_staleness_window_hours()
                staleness_window_seconds = staleness_window_hours * 3600
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action = 'scheduled_staleness' "
                    f"AND CAST(strftime('%s', occurred_at) AS INTEGER) >= CAST(strftime('%s', 'now') - {staleness_window_seconds} AS INTEGER)"
                ).fetchone()
                if not row[0]:
                    issues.append("Stale detection missed last night")

                # 3. Are there recent consecutive errors from the same source?
                # Uses epoch math to avoid separator bugs (occurred_at is
                # 'T'-separated, datetime('now') is space-separated).
                rows = conn.execute(
                    "SELECT action, COUNT(*) as cnt FROM user_activity "
                    "WHERE json_extract(metadata, '$.status') = 'failed' "
                    "AND CAST(strftime('%s', occurred_at) AS INTEGER) >= "
                    "CAST(strftime('%s', 'now') - 86400 AS INTEGER) "
                    "GROUP BY action HAVING cnt >= 5"
                ).fetchall()
                for r in rows:
                    issues.append(f"{r[0]}: {r[1]} failures in 24h")

                # 4. OAuth token validity
                try:
                    from job_finder.gmail_auth import get_credentials

                    get_credentials()
                except Exception as e:
                    issues.append(f"OAuth token invalid: {e}")

                # 5. Owner-idle alarm: the app running unattended while the owner
                #    has drifted away (pre-mortem #1). Read-only.
                owner_idle = _check_owner_idle(conn, config)
                if owner_idle:
                    issues.append(owner_idle)

                # 6. Score-rot parity: stored verdicts that today's rule would
                #    change (pre-mortem #3). Read-only, zero model calls.
                score_rot = _check_score_rot(conn, config)
                if score_rot:
                    issues.append(score_rot)

                # 7. Funnel unexplained-drop: silent-drop bug detection (issue #587).
                #    Read-only, checks persisted funnel metadata from last ingestion.
                funnel_unexplained = _check_funnel_unexplained(conn, config)
                if funnel_unexplained:
                    issues.append(funnel_unexplained)

                # 8. Concentration alarm: surfaced fit-floor cohort diversity erosion
                #    (issue #592). Read-only, checks normalized HHI for employer/platform.
                concentration = _check_concentration(conn, config)
                if concentration:
                    issues.append(concentration)

                # 9. Conversion-signal: does the fit-grade predict real outcomes?
                #    Read-only, checks per-band application- and callback-rate.
                conversion = _check_conversion_signal(conn, config)
                if conversion:
                    issues.append(conversion)

                # 10. Cost-ledger free/paid health watch (Detector C): read-only
                #     check of the scoring_costs ledger for paid leaks and broken
                #     free rung.
                cost_health = _check_cost_health(conn, config)
                if cost_health:
                    issues.append(cost_health)

                # 11. Supply-side per-source deadman: detects silent source failures
                #     (issue #588). Read-only, checks ATS scanner fleet and
                #     company_scan_log age against the fixed daily ATS-scan window.
                source_deadman_issues = _check_source_deadman(conn, config)
                issues.extend(source_deadman_issues)

        except Exception as e:
            issues.append(f"Health check DB error: {e}")

        # 7. Autoheal: retry heals for still-degraded sources (run_heal gates
        #    flag/backoff/attempt-cap itself) + attempt-counter hygiene. The
        #    sweep never contributes to `issues` — it must not fail the heartbeat.
        try:
            from job_finder.web.autoheal.heal_pipeline import run_heal
            from job_finder.web.db_helpers import standalone_connection as _sc

            reset_days = float(config.get("autoheal", {}).get("heal_attempt_reset_days", 30))
            with _sc(db_path) as conn:
                # Hygiene backstop (plan invariant I1): a source healthy for
                # 30+ days since its last heal gets its attempt budget back
                # even while an override is active.
                conn.execute(
                    "UPDATE source_health SET heal_attempts = 0 "
                    "WHERE status = 'healthy' AND heal_attempts > 0 "
                    "AND last_heal_at IS NOT NULL "
                    "AND last_heal_at < datetime('now', ?)",
                    (f"-{reset_days} days",),
                )
                conn.commit()
                degraded = [
                    r[0]
                    for r in conn.execute(
                        "SELECT source FROM source_health WHERE status = 'degraded'"
                    ).fetchall()
                ]
                degraded_sources = list(degraded)
                for source in degraded:
                    try:
                        run_heal(conn, config, source)
                    except Exception:
                        logger.exception("health-check heal retry failed for %s", source)
        except Exception:
            logger.exception("health-check autoheal sweep failed")

        status = "degraded" if issues else "success"
        if issues:
            logger.warning("HEALTH_DEGRADED: %s", "; ".join(issues))
        else:
            logger.info("HEALTH_OK: ingestion, stale detection, OAuth all nominal")

        # 8. Escalation: a signal degraded for N consecutive heartbeats fires the
        #    notification egress. Runs every heartbeat (even when nominal) so
        #    recovered keys get their streak counter reset to 0. Best-effort —
        #    must never fail the heartbeat.
        try:
            _escalate_degradation(
                db_path,
                issues,
                degraded_sources,
                config,
                datetime.now(UTC).replace(tzinfo=None).isoformat(),
            )
        except Exception:
            logger.exception("health-check escalation tracking failed")

        log_activity(
            db_path,
            ACTION_SCHEDULED_HEALTH,
            metadata={"status": status, "issues": issues},
        )
        run_events.end(
            run_id,
            job="health",
            source="scheduler",
            disposition="degraded" if issues else "completed",
            db_path=db_path,
            duration_s=round(_time.time() - t0, 2),
            result={"issues": issues},
        )


def run_jd_adjudication(db_path: str, config: dict) -> dict:
    """Scheduled entry point: LLM-adjudicate a bounded batch of AMBIGUOUS jd_full rows.

    Opens its own connection (scheduler thread, not Flask g.db) and drains up to a
    bounded batch of the jd-content AMBIGUOUS middle through the local-LLM
    tie-breaker (PR2 of the jd-content contract). Returns the backfill summary dict
    for run logging / dashboard metadata.
    """
    from job_finder.web.db_helpers import standalone_connection
    from job_finder.web.jd_adjudicator import run_jd_adjudication_backfill

    with standalone_connection(db_path) as conn:
        return run_jd_adjudication_backfill(conn, config, limit=1000)
