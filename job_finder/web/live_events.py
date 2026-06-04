"""In-process pub/sub bus for live UI updates (the SSE backbone).

Flask request handlers and the APScheduler background jobs run in the *same*
process, so a simple in-memory fan-out bridges them: background work publishes
a semantic event, and every connected browser's SSE stream (see
``job_finder.web.blueprints.events``) drains its own queue and forwards the
event. HTMX widgets listen via ``hx-trigger="sse:<event>"`` and refetch their
fragment route.

Scale is single-user/local: a handful of subscribers at most (one per open
tab). Per-subscriber queues are *bounded* so a stalled/dead connection drops
events rather than growing without limit; the keepalive in the SSE endpoint
reaps dead subscribers within seconds anyway.
"""

from __future__ import annotations

import logging
import queue
import threading

logger = logging.getLogger(__name__)

# Semantic event names. A widget subscribes to the event(s) that reflect the
# data it displays; a publisher emits the event(s) whose underlying data it
# just mutated. Keep this list small — coarse events keep instrumentation
# maintainable and the refetches are cheap local SQLite reads.
JOBS_CHANGED = "jobs-changed"
COMPANIES_CHANGED = "companies-changed"
COSTS_CHANGED = "costs-changed"
PIPELINE_CHANGED = "pipeline-changed"
DETECTIONS_CHANGED = "detections-changed"

# Per-subscriber backlog before we start dropping. A live browser drains within
# milliseconds; this only bounds memory for a wedged/zombie connection.
_MAX_QUEUED = 64


class LiveEventBus:
    """Thread-safe fan-out of event names to per-subscriber queues."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[str]:
        """Register a new subscriber and return its delivery queue."""
        q: queue.Queue[str] = queue.Queue(maxsize=_MAX_QUEUED)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        """Remove a subscriber (idempotent)."""
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str) -> None:
        """Deliver ``event`` to every current subscriber (non-blocking)."""
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                logger.debug("live bus: subscriber backlog full, dropped %s", event)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Module-level singleton — reachable from both request and scheduler threads
# without threading it through the Flask app object.
live_bus = LiveEventBus()


def publish(event: str) -> None:
    """Module-level convenience wrapper around the singleton bus.

    Safe to call from any thread and never raises on the caller's behalf — a
    live-update notification must never break the data mutation that triggered
    it.
    """
    try:
        live_bus.publish(event)
    except Exception:
        logger.debug("live bus: publish(%s) failed", event, exc_info=True)
