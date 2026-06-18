"""Serve-path liveness heartbeat.

The running app touches a single ``last_alive`` file under the user-data root
on a short recurring interval so an out-of-process healthcheck (the
``job-cannon healthcheck`` CLI) can judge liveness by *freshness* — even when
the in-process scheduler's daily health heartbeat has not fired and the HTTP
listener cannot be reached.

Why a file and not a DB row: the write happens every ``HEARTBEAT_INTERVAL_S``
seconds, and a per-minute ``user_activity`` insert would compete for the WAL
write lock against real ingestion/scoring writers for no benefit. A touched
file is the cheapest possible signal, needs no DB lock, and stays readable even
if SQLite is wedged.

Consumer staleness rule (owned and enforced by the healthcheck, not here): the
marker is considered stale after ``stale_after_seconds()`` — ``max(2 *
HEARTBEAT_INTERVAL_S, _STALE_FLOOR_S)`` — i.e. two missed ticks plus a floor for
clock skew / GC pauses. With the default 60s interval that threshold is 120s.
Consumers should import and call ``stale_after_seconds()`` rather than
re-deriving the rule, so the threshold has a single source of truth.
"""

from __future__ import annotations

import logging
import os

from job_finder.json_utils import utc_now_iso
from job_finder.web import user_data_dirs

logger = logging.getLogger(__name__)

# Heartbeat cadence in seconds. The interval job is registered with this period
# in ``scheduler/_jobs.py:register_heartbeat``.
HEARTBEAT_INTERVAL_S = 60

# Floor for the consumer's staleness threshold; keeps the threshold sane even
# if the interval is ever lowered. Threshold = max(2 * HEARTBEAT_INTERVAL_S, floor).
_STALE_FLOOR_S = 120


def stale_after_seconds() -> int:
    """Return the freshness window (seconds) after which ``last_alive`` is stale.

    ``max(2 * HEARTBEAT_INTERVAL_S, _STALE_FLOOR_S)`` — two missed ticks plus a
    floor for clock skew / GC pauses. The healthcheck imports and calls this so
    the staleness rule lives in exactly one place.
    """
    return max(2 * HEARTBEAT_INTERVAL_S, _STALE_FLOOR_S)


def write_heartbeat() -> None:
    """Atomically refresh the ``last_alive`` marker with the current UTC time.

    Writes a fresh naive-UTC ISO timestamp (``utc_now_iso``) to a sibling temp
    file and ``os.replace``-s it onto the target, so a concurrent reader never
    sees a torn write. Best-effort: never raises — on any IO error the failure
    is logged at debug and the function returns ``None`` so a wedged disk can
    never crash the scheduler thread.
    """
    try:
        target = user_data_dirs.last_alive_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # PID in the temp name keeps it deterministic per process (no unbounded
        # litter on repeated failures) while avoiding any cross-process clash.
        tmp = target.parent / f"{target.name}.{os.getpid()}.tmp"
        tmp.write_text(utc_now_iso(), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        logger.debug("heartbeat write failed", exc_info=True)
