"""Centralized scheduling descriptor table — the single source of truth for
*when* every background job runs and *how* the cadence preset drives it.

WHY THIS EXISTS
---------------
Before this module the schedule was scattered across the ``register_*``
helpers in ``_jobs.py``: each hard-coded its own ``CronTrigger(hour=..., minute=...)``.
That made three latent bugs invisible at the call site:

  (a) **Staleness overruns its window.** ``staleness_check`` (Phase C runs a
      parallel HTTP cascade with a per-job timeout) can run ~2h, drifting from
      its 2:00 slot toward the 4:15 agentic-backfill window. A bounded
      ``misfire_grace_time`` keeps a *late start* from piling up across days;
      spacing keeps the *run* clear of its neighbours.

  (b) **05:00 collision.** ``careers_crawl`` and ``company_linkage`` both fired
      at 05:00 and both write the companies/jobs tables — a genuine
      heavy-writer slot collision that DB-locked under contention (the same
      failure mode that killed the 3:30 agentic run, see #229).

  (c) **Cadence config covered only ingestion.** ``cadence_preset`` resized the
      ingestion cron but enrichment_backfill kept a hard-coded ``1,9,17`` — so
      ``light`` / ``heavy`` silently desynced ingestion from its own backfill.

This table makes the schedule declarative and enforces an invariant at boot:
**no two heavy-writer jobs may share an hour:minute slot.** Dependent pairs
(``careers_crawl → company_linkage``, ``staleness_check → agentic_backfill``)
are expressed as ``depends_on`` and chained at completion rather than racing on
a shared cron slot.

APScheduler 3.x only (pinned <4.0). No 4.x ``add_schedule`` / ``CoalescePolicy``.
"""

from __future__ import annotations

from dataclasses import dataclass

# How long (seconds) a job may start late before APScheduler treats the fire as
# a misfire and (with coalesce=True) drops it. Heavy nightly jobs get a generous
# window so a slightly-late scheduler thread still runs them; but it is bounded
# so a long-overrunning predecessor cannot let a fire silently accumulate for
# hours and then double-run. 1 hour is comfortably inside every gap below.
HEAVY_MISFIRE_GRACE_S = 3600

# Light / frequent jobs (pipeline_detection every 30 min) want a tight grace so
# a missed tick is dropped rather than replayed late on top of the next tick.
LIGHT_MISFIRE_GRACE_S = 300


@dataclass(frozen=True)
class JobSlot:
    """Declarative schedule descriptor for one background job.

    Attributes:
        hour / minute: CronTrigger fields. ``None`` for both means the job has
            no standalone cron trigger — it runs only when chained off a
            predecessor (``depends_on``). ``hour`` may be a multi-value cron
            expression string ("1,9,17") for jobs that derive from a cadence.
        heavy_writer: True if the job performs substantial writes to the
            jobs/companies tables. Two heavy writers may NOT share a slot; the
            boot-time assertion enforces this.
        depends_on: job_id of a predecessor. If set, this job is scheduled as a
            one-shot when the predecessor finishes (completion-chaining) instead
            of on its own cron slot.
        misfire_grace_time: seconds; see module constants.
    """

    hour: int | str | None
    minute: int | None
    heavy_writer: bool = False
    depends_on: str | None = None
    misfire_grace_time: int = HEAVY_MISFIRE_GRACE_S


# ---------------------------------------------------------------------------
# The schedule. Cadence-derived hours (ingestion, enrichment_backfill) carry a
# sentinel hour=None-with-special-handling: register_ingestion /
# register_enrichment_backfill compute their cron from the preset and do not
# read .hour here. They still appear in the table so the collision assertion
# and the descriptor-completeness test see every job id.
#
# Slot map (local time), non-cadence jobs only — verify visually no two
# heavy_writer rows share a slot:
#
#   02:00  staleness_check        (heavy)  → chains agentic_backfill on finish
#   03:00  orphan_cleanup         (day=1)
#   03:30  registry_hygiene       (day=1)
#   04:15  agentic_backfill       (heavy, chained off staleness_check)
#   04:45  ats_source_url_promote
#   05:00  careers_crawl          (heavy)  → chains company_linkage on finish
#   05:45  primary_source_res.    (heavy)
#   06:00  health_heartbeat
#   06:30  homepage_discovery
#   07:00  ats_scan               (heavy)
#   07:30  ats_slug_probe
#   --:--  pipeline_detection     (every 30 min interval)
#   company_linkage              (heavy, chained off careers_crawl)
# ---------------------------------------------------------------------------

SCHEDULE: dict[str, JobSlot] = {
    # Cadence-derived (hour computed from preset; minute defaults 0).
    "ingestion_poll": JobSlot(hour=None, minute=0, heavy_writer=True),
    "enrichment_backfill": JobSlot(hour=None, minute=0, heavy_writer=True),
    # Nightly fixed-slot jobs.
    "staleness_check": JobSlot(hour=2, minute=0, heavy_writer=True),
    "agentic_backfill": JobSlot(
        hour=None, minute=None, heavy_writer=True, depends_on="staleness_check"
    ),
    "ats_source_url_promote": JobSlot(hour=4, minute=45),
    "careers_crawl": JobSlot(hour=5, minute=0, heavy_writer=True),
    "company_linkage": JobSlot(
        hour=None, minute=None, heavy_writer=True, depends_on="careers_crawl"
    ),
    "primary_source_resolution": JobSlot(hour=5, minute=45, heavy_writer=True),
    "health_heartbeat": JobSlot(hour=6, minute=0),
    "homepage_discovery": JobSlot(hour=6, minute=30),
    "ats_scan": JobSlot(hour=7, minute=0, heavy_writer=True),
    "ats_slug_probe": JobSlot(hour=7, minute=30),
    # Monthly hygiene (day=1).
    "orphan_cleanup": JobSlot(hour=3, minute=0),
    "registry_hygiene": JobSlot(hour=3, minute=30),
    # Frequent interval job.
    "pipeline_detection": JobSlot(
        hour=None, minute=None, misfire_grace_time=LIGHT_MISFIRE_GRACE_S
    ),
}


# ---------------------------------------------------------------------------
# Cadence preset → cron hour expressions (single source of truth).
#
# Ingestion and its enrichment backfill must stay coupled: the backfill runs
# one hour after each ingestion slot so freshly-ingested rows get jd_full +
# scoring same-cycle. Deriving both from one preset is the whole point of (c).
# ---------------------------------------------------------------------------

_INGESTION_HOURS: dict[str, str] = {
    "light": "8",
    "standard": "0,8,16",
    "heavy": "0,4,8,12,16,20",
}


def ingestion_hour_expr(preset: str) -> str:
    """Map a cadence preset to the ingestion CronTrigger hour expression.

    Unknown / missing presets fall back to ``standard`` (0,8,16) so existing
    deployments that omit ``cadence_preset`` are unaffected.
    """
    return _INGESTION_HOURS.get(preset, _INGESTION_HOURS["standard"])


def enrichment_hour_expr(preset: str) -> str:
    """Enrichment-backfill hours = each ingestion hour + 1 (mod 24), sorted.

    light    8        → 9
    standard 0,8,16   → 1,9,17   (the long-documented coupling, now derived)
    heavy    every 4h → 1,5,9,13,17,21
    """
    ingestion = ingestion_hour_expr(preset)
    hours = sorted((int(h) + 1) % 24 for h in ingestion.split(","))
    return ",".join(str(h) for h in hours)


# ---------------------------------------------------------------------------
# Boot-time collision guard.
# ---------------------------------------------------------------------------


def assert_no_heavy_writer_collisions(schedule: dict[str, JobSlot] | None = None) -> None:
    """Raise AssertionError if two heavy-writer jobs share a fixed hour:minute slot.

    Cadence-derived (hour=None, minute set) and chained (depends_on set, both
    None) jobs are excluded — they have no standalone fixed slot to collide on.
    Called once at scheduler boot as a guard against a future descriptor edit
    that reintroduces the 05:00-style contention.
    """
    sched = SCHEDULE if schedule is None else schedule
    seen: dict[tuple[int, int], str] = {}
    for job_id, slot in sched.items():
        if not slot.heavy_writer:
            continue
        if slot.depends_on is not None:
            continue
        # A fixed slot needs a concrete integer hour AND minute.
        if not isinstance(slot.hour, int) or not isinstance(slot.minute, int):
            continue
        key = (slot.hour, slot.minute)
        if key in seen:
            raise AssertionError(
                f"heavy-writer slot collision at {slot.hour:02d}:{slot.minute:02d}: "
                f"'{seen[key]}' and '{job_id}' would contend on the same DB tables. "
                f"Space them or express one as depends_on the other."
            )
        seen[key] = job_id
