"""Tests for the SSE live-update backbone.

Covers the in-process :class:`LiveEventBus` fan-out, the ``/events`` stream
endpoint, and the SSE-driven fragment routes added for live updates.
"""

import queue

import pytest

from job_finder.web.live_events import (
    _MAX_QUEUED,
    JOBS_CHANGED,
    LiveEventBus,
    publish,
)


class TestLiveEventBus:
    def test_publish_delivers_to_subscriber(self):
        bus = LiveEventBus()
        q = bus.subscribe()
        bus.publish(JOBS_CHANGED)
        assert q.get_nowait() == JOBS_CHANGED

    def test_fans_out_to_all_subscribers(self):
        bus = LiveEventBus()
        q1, q2, q3 = bus.subscribe(), bus.subscribe(), bus.subscribe()
        bus.publish("companies-changed")
        assert q1.get_nowait() == "companies-changed"
        assert q2.get_nowait() == "companies-changed"
        assert q3.get_nowait() == "companies-changed"

    def test_unsubscribe_stops_delivery(self):
        bus = LiveEventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish(JOBS_CHANGED)
        with pytest.raises(queue.Empty):
            q.get_nowait()
        assert bus.subscriber_count == 0

    def test_unsubscribe_is_idempotent(self):
        bus = LiveEventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.unsubscribe(q)  # must not raise

    def test_bounded_queue_drops_without_raising(self):
        """A wedged subscriber must never make publish() raise or block."""
        bus = LiveEventBus()
        q = bus.subscribe()
        # Overfill well past the bound; publish must stay non-blocking.
        for _ in range(_MAX_QUEUED + 50):
            bus.publish(JOBS_CHANGED)
        # Queue is capped, not unbounded.
        assert q.qsize() <= _MAX_QUEUED

    def test_module_publish_never_raises(self):
        # The module-level helper swallows everything — a notification must
        # never break the data mutation that triggered it.
        publish(JOBS_CHANGED)  # no subscribers, no error


class TestEventsEndpoint:
    def test_stream_headers_and_event_delivery(self, app):
        """/events streams text/event-stream and forwards a published event."""
        from job_finder.web.blueprints.events import stream

        with app.test_request_context("/events"):
            resp = stream()
            assert resp.mimetype == "text/event-stream"
            assert resp.headers["Cache-Control"] == "no-cache"

            gen = iter(resp.response)
            prelude = next(gen)
            assert prelude.startswith("retry:")

            # Subscriber now exists (created when the generator started); a
            # publish must surface as a named SSE message with a data line.
            publish(JOBS_CHANGED)
            msg = next(gen)
            assert "event: jobs-changed" in msg
            assert "data: ok" in msg

            resp.response.close()  # triggers finally -> unsubscribe


class TestLiveFragmentRoutes:
    """The fragment routes the SSE triggers GET must return 200 + their markup."""

    def test_review_queue_fragment(self, client):
        resp = client.get("/dashboard/review-queue", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # OOB header badge is part of the response so the count stays in sync.
        assert 'id="pipeline-review-header"' in resp.data.decode()

    def test_companies_health_fragment(self, client):
        resp = client.get("/companies/health", headers={"HX-Request": "true"})
        assert resp.status_code == 200

    def test_pipeline_board_fragment(self, client):
        resp = client.get("/pipeline/board", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # Column bodies must carry the SortableJS hook class so re-init works.
        assert "kanban-column-body" in resp.data.decode()
