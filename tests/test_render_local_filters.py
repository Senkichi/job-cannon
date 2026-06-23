"""Unit tests for the render-local Jinja filters (store-UTC / render-local).

Storage contract (``arch-store-utc-render-local``): all DB timestamps are
naive UTC ISO strings; the display layer converts to OS-local at render time
via these filters. ``local_datetime`` was added (Phase 46 wiring sweep) for the
dashboard activity/history tables and the pipeline-event timelines, which show
clock time — the date-only ``local_date`` would have dropped the HH:MM, and the
prior ``ts[:16] | replace('T',' ')`` template slices rendered the stored UTC
wall-clock as if it were local.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from job_finder.web.blueprints.jobs import (
    local_date,
    local_datetime,
    relative_date,
)


def _ref_local(iso: str, fmt: str) -> str:
    """Reference UTC->OS-local conversion, mirroring _parse_stored_ts_as_local.

    Computed with the same astimezone() the filter uses, so the assertion is
    deterministic on any machine's local timezone while still pinning that the
    filter performs the conversion (not a raw slice) and uses the expected fmt.
    """
    return (
        datetime.fromisoformat(iso)
        .replace(tzinfo=UTC)
        .astimezone()
        .replace(tzinfo=None)
        .strftime(fmt)
    )


# ---------------------------------------------------------------------------
# local_datetime — the new date+time filter
# ---------------------------------------------------------------------------


def test_local_datetime_converts_utc_to_local_with_time():
    iso = "2026-03-15T14:30:00"
    assert local_datetime(iso) == _ref_local(iso, "%Y-%m-%d %H:%M")


def test_local_datetime_shape_is_minute_precision():
    """Output is 'YYYY-MM-DD HH:MM' — no seconds, no 'T'."""
    out = local_datetime("2026-03-15T14:30:45")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out), out


def test_local_datetime_date_portion_matches_local_date():
    """local_datetime and local_date must agree on the calendar date so the two
    views never disagree about which local day an event fell on."""
    iso = "2026-03-15T23:30:00"  # late-evening UTC — most likely to cross midnight
    assert local_datetime(iso)[:10] == local_date(iso)


def test_local_datetime_handles_tz_aware_input():
    """Legacy rows / external-API responses may carry an explicit +00:00 or Z."""
    aware = "2026-03-15T14:30:00+00:00"
    zulu = "2026-03-15T14:30:00Z"
    naive = "2026-03-15T14:30:00"
    assert local_datetime(aware) == local_datetime(naive)
    assert local_datetime(zulu) == local_datetime(naive)


def test_local_datetime_empty_and_none_return_blank():
    assert local_datetime("") == ""
    assert local_datetime(None) == ""


def test_local_datetime_malformed_falls_back_to_slice():
    """Unparseable input degrades to the old [:16] | replace('T',' ') slice,
    never raises (display path must never 500 on a junk timestamp)."""
    assert local_datetime("not-a-timestamp") == "not-a-timestamp"[:16].replace("T", " ")


# ---------------------------------------------------------------------------
# local_date / relative_date — companions (previously untested)
# ---------------------------------------------------------------------------


def test_local_date_converts_utc_to_local():
    iso = "2026-03-15T14:30:00"
    assert local_date(iso) == _ref_local(iso, "%Y-%m-%d")


def test_local_date_empty_returns_blank():
    assert local_date("") == ""
    assert local_date(None) == ""


def test_relative_date_includes_absolute_and_relative():
    """Format contract (locked): 'Mon D (Nd ago)'."""
    out = relative_date("2026-03-15T14:30:00")
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} \(.+\)", out), out


def test_relative_date_empty_returns_placeholder():
    assert relative_date("") == "---"
    assert relative_date(None) == "---"
