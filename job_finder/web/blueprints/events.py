"""Server-Sent Events endpoint — streams live-update notifications to browsers.

HTMX's SSE extension opens one ``EventSource`` per page view to ``/events``.
We subscribe that connection to the in-process :data:`live_bus` and forward
every published event as a named SSE message, so widgets wired with
``hx-trigger="sse:<event>"`` refetch their fragment the instant a background
job (or a completed user operation) mutates the underlying data.

Requires the dev server to run threaded (``app.run(threaded=True)``): each open
stream holds one worker thread for the life of the connection.
"""

from __future__ import annotations

import logging
import queue

from flask import Blueprint, Response, stream_with_context

from job_finder.web.live_events import live_bus

logger = logging.getLogger(__name__)

events_bp = Blueprint("events", __name__)

# Block this long waiting for an event before emitting a keepalive comment.
# The periodic write is also how we notice a client disconnect (the write
# fails, the generator is closed, and the subscriber is reaped in `finally`).
_KEEPALIVE_SECONDS = 15


@events_bp.route("/events", strict_slashes=False)
def stream() -> Response:
    """Long-lived ``text/event-stream`` forwarding live-bus events."""

    def _gen():
        q = live_bus.subscribe()
        try:
            # Prelude opens the stream and sets the client reconnect backoff.
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = q.get(timeout=_KEEPALIVE_SECONDS)
                except queue.Empty:
                    # Comment line: keeps the socket warm and forces a write
                    # that surfaces a dropped connection.
                    yield ": keepalive\n\n"
                    continue
                # A `data:` line is mandatory — the SSE spec drops messages
                # whose data buffer is empty, even when an event name is set.
                yield f"event: {event}\ndata: ok\n\n"
        finally:
            live_bus.unsubscribe(q)

    resp = Response(stream_with_context(_gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp
