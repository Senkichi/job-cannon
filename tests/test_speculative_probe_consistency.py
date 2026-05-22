"""F6 — speculative-probe careers_url consistency check.

Catches brand-name-collision false positives where the speculative probe
hits one ATS (because the slug happens to exist there) while the company's
own `careers_url` positively identifies a DIFFERENT ATS.

Honest limit: this fix does NOT catch the Shopify case (careers_url=
shopify.com/careers carries no ATS signature). That requires wide F6
(fetch and parse careers page), deferred.
"""

import os
import sqlite3
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

from job_finder.web.ats_detection import (
    careers_url_is_live,
    probe_hit_consistent_or_dead_url,
    probe_hit_consistent_with_careers_url,
)
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fake HTTP response — lets us drive careers_url_is_live without real network.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_get(status: int):
    def _get(url, timeout):
        return _FakeResp(status)

    return _get


def _raising_get(exc: Exception):
    def _get(url, timeout):
        raise exc

    return _get


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def _insert_pending_company(
    conn: sqlite3.Connection,
    name: str,
    careers_url: str | None = None,
) -> int:
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
           (name, name_raw, careers_url, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, ?, 'pending', ?, ?)""",
        (name.lower(), name, careers_url, now, now),
    )
    conn.commit()
    inserted_id = cursor.lastrowid
    assert inserted_id is not None
    return inserted_id


# ---------------------------------------------------------------------------
# Unit tests — probe_hit_consistent_with_careers_url
# ---------------------------------------------------------------------------


class TestProbeHitConsistencyHelper:
    """Pure-function tests for the helper, independent of the probe loop."""

    def test_no_careers_url_is_consistent(self):
        """Without a careers_url, we have nothing to disprove the hit."""
        assert probe_hit_consistent_with_careers_url("pinpoint", None) is True
        assert probe_hit_consistent_with_careers_url("pinpoint", "") is True

    def test_careers_url_with_no_ats_signature_is_consistent(self):
        """careers_url like 'shopify.com/careers' carries no ATS signature.

        The helper passes the hit through. This is the documented narrow-F6
        limitation: wide F6 (fetch + widget parse) is needed to catch this.
        """
        assert (
            probe_hit_consistent_with_careers_url("pinpoint", "https://shopify.com/careers")
            is True
        )

    def test_url_inferred_platform_matches_hit_is_consistent(self):
        """Greenhouse URL + greenhouse hit → accept."""
        assert (
            probe_hit_consistent_with_careers_url(
                "greenhouse", "https://boards.greenhouse.io/acme"
            )
            is True
        )

    def test_url_inferred_platform_differs_from_hit_is_rejected(self):
        """Lever URL + greenhouse hit → reject — the Shopify-style pathology
        with a positive URL signature.
        """
        assert (
            probe_hit_consistent_with_careers_url("greenhouse", "https://jobs.lever.co/acme")
            is False
        )

    def test_ashby_url_rejects_other_platform_hit(self):
        assert (
            probe_hit_consistent_with_careers_url("pinpoint", "https://jobs.ashbyhq.com/acme")
            is False
        )

    def test_workday_url_accepts_workday_hit(self):
        """Workday subdomain pattern matches; same-platform hit passes."""
        assert (
            probe_hit_consistent_with_careers_url(
                "workday", "https://zillow.wd5.myworkdayjobs.com/External"
            )
            is True
        )


# ---------------------------------------------------------------------------
# Integration test — probe_ats_slugs honors the consistency gate
# ---------------------------------------------------------------------------


def _build_probes(hits_for: dict[str, bool]) -> list:
    """Build a fake _PROBES list. `hits_for` maps platform name → True/False.

    Uses the same (name, callable) shape probe_ats_slugs expects. Direct
    list replacement is needed because the real _PROBES captures function
    references at import time — patching the names doesn't reach them.
    """
    all_platforms = [
        "lever",
        "greenhouse",
        "ashby",
        "recruitee",
        "breezy",
        "jazzhr",
        "pinpoint",
        "teamtailor",
        "personio",
        "bamboohr",
    ]

    def _make_probe(value: bool):
        def _probe(_slug):
            return value

        return _probe

    return [(name, _make_probe(hits_for.get(name, False))) for name in all_platforms]


class TestProbeAtsSlugsConsistencyGate:
    """End-to-end: a probe that hits but disagrees with careers_url is
    rejected; the company stays on miss."""

    def test_hit_with_mismatched_careers_url_is_rejected(self, migrated_db_path):
        """Lever URL (LIVE) + speculative pinpoint hit → company ends on miss.

        Liveness explicitly mocked to True so the test asserts the
        brand-collision path (Shopify-style with a positive URL signature).
        Without the mock, the lever URL would hit real network — flakey and
        the wrong intent anyway.
        """
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/some-other-acme",
        )
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
            patch(
                "job_finder.web.ats_detection.careers_url_is_live",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["probed"] == 1
        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None
        assert row["ats_slug"] is None

    def test_hit_with_matching_careers_url_is_accepted(self, migrated_db_path):
        """Greenhouse URL + speculative greenhouse hit → company promoted."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://boards.greenhouse.io/acme",
        )
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "greenhouse"

    def test_hit_with_no_careers_url_is_accepted(self, migrated_db_path):
        """Without a careers_url, the gate is silent — pre-F6 behavior."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"lever": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "lever"

    def test_rejected_hit_falls_through_to_legitimate_hit_on_next_platform(self, migrated_db_path):
        """When the first hit is rejected by the gate but a later platform
        ALSO hits and IS consistent, the consistent one wins.
        """
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            # URL infers greenhouse → pinpoint hit rejected, greenhouse accepted.
            careers_url="https://boards.greenhouse.io/acme",
        )
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True, "greenhouse": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
            # Block real HTTP from careers_url_is_live; force "live" so the
            # pinpoint rejection holds and greenhouse takes over.
            patch(
                "job_finder.web.ats_detection.careers_url_is_live",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        conn.close()
        assert row["ats_platform"] == "greenhouse"


# ---------------------------------------------------------------------------
# Unit tests — careers_url_is_live
# ---------------------------------------------------------------------------


class TestCareersUrlIsLive:
    """Pure unit tests via injected `_get`. No real HTTP."""

    def test_none_url_returns_none(self):
        assert careers_url_is_live(None) is None

    def test_empty_url_returns_none(self):
        assert careers_url_is_live("") is None

    def test_200_returns_true(self):
        assert careers_url_is_live("https://x/", _get=_make_get(200)) is True

    def test_204_returns_true(self):
        """Any 2xx is treated as live (defensive — most ATSes return 200)."""
        assert careers_url_is_live("https://x/", _get=_make_get(204)) is True

    def test_404_returns_false(self):
        """404 is the canonical signal that an ATS tenant has been removed."""
        assert careers_url_is_live("https://x/", _get=_make_get(404)) is False

    def test_410_returns_false(self):
        """410 Gone — explicit signal that the resource is permanently dead."""
        assert careers_url_is_live("https://x/", _get=_make_get(410)) is False

    def test_403_returns_none(self):
        """Ambiguous — bot block, paywall, or legitimately gated. Caller
        falls back to conservative gate behavior (preserve rejection).
        """
        assert careers_url_is_live("https://x/", _get=_make_get(403)) is None

    def test_500_returns_none(self):
        """Server-side fault. Could be transient — don't trust either way."""
        assert careers_url_is_live("https://x/", _get=_make_get(500)) is None

    def test_exception_returns_none(self):
        """Timeout, DNS failure, connection refused — all undetermined."""
        assert (
            careers_url_is_live("https://x/", _get=_raising_get(TimeoutError("timeout"))) is None
        )


# ---------------------------------------------------------------------------
# Unit tests — probe_hit_consistent_or_dead_url (composite)
# ---------------------------------------------------------------------------


class TestProbeHitConsistentOrDeadUrl:
    """Composite of the pure helper + liveness check. Tests inject the
    liveness_check callable so no network is touched.
    """

    def test_no_url_short_circuits_no_liveness_check_made(self):
        """careers_url=None → pure helper accepts → liveness never called."""
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert probe_hit_consistent_or_dead_url("pinpoint", None, liveness_check=_spy) is True
        assert calls == []

    def test_matching_platform_short_circuits_no_liveness_check_made(self):
        """Matching platform → pure helper accepts → liveness never called."""
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert (
            probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://boards.greenhouse.io/acme",
                liveness_check=_spy,
            )
            is True
        )
        assert calls == []

    def test_no_signature_short_circuits_no_liveness_check_made(self):
        """careers_url with no ATS signature → pure helper accepts → liveness
        never called. Documents the narrow-F6 limit (Shopify case still slips).
        """
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://shopify.com/careers",
                liveness_check=_spy,
            )
            is True
        )
        assert calls == []

    def test_mismatched_but_live_url_rejects_hit(self):
        """Brand-collision case: careers_url positively identifies a different
        platform AND is live → keep the rejection. This is the original F6
        behavior preserved.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://jobs.lever.co/some-other-acme",
                liveness_check=lambda _u: True,
            )
            is False
        )

    def test_mismatched_but_dead_url_accepts_hit(self):
        """Migration case: careers_url is 404/410 → trust the live probe hit.
        Matches the real Nimble Robotics + Niantic findings from the audit.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://jobs.lever.co/NimbleAI",  # 404 in production
                liveness_check=lambda _u: False,
            )
            is True
        )

    def test_mismatched_and_ambiguous_url_preserves_rejection(self):
        """Conservative default: 5xx/403/timeout → can't confirm dead, so we
        keep the rejection. Prevents the Shopify pathology from leaking
        through when the careers_url happens to be temporarily blocked.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://jobs.lever.co/some-other-acme",
                liveness_check=lambda _u: None,
            )
            is False
        )

    def test_default_liveness_check_is_careers_url_is_live(self):
        """Smoke: when liveness_check is not provided, the composite reaches
        for `careers_url_is_live`. Patch it at the module to confirm wiring.
        """
        with patch(
            "job_finder.web.ats_detection.careers_url_is_live", return_value=False
        ) as mock_check:
            result = probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://jobs.lever.co/some-other-acme",
            )
            assert result is True
            mock_check.assert_called_once_with("https://jobs.lever.co/some-other-acme")


# ---------------------------------------------------------------------------
# Integration test — migration scenario through probe_ats_slugs
# ---------------------------------------------------------------------------


class TestMigrationScenario:
    """End-to-end: a company with a stale (404) careers_url should still get
    promoted when the live probe rediscovers the new ATS — F6 narrow's
    original bug, now fixed by the liveness augmentation.
    """

    def test_stale_careers_url_does_not_block_probe(self, migrated_db_path):
        """Nimble Robotics-style: careers_url is jobs.lever.co/X but the
        Lever tenant 404s; meanwhile the company's new ATS is greenhouse.
        Pre-augmentation F6 would reject. Post-augmentation, the greenhouse
        hit wins.
        """
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/StaleAcme",  # 404 in production
        )
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
            # Force the careers_url to be "dead" — simulates the 404 we saw
            # for jobs.lever.co/NimbleAI in production.
            patch(
                "job_finder.web.ats_detection.careers_url_is_live",
                return_value=False,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "greenhouse"

    def test_live_mismatched_careers_url_still_blocks_probe(self, migrated_db_path):
        """Shopify-style (hypothetical with live URL): careers_url positively
        identifies Lever and IS live → speculative pinpoint hit gets rejected.
        Confirms augmentation hasn't weakened the brand-collision protection.
        """
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/RealAcme",
        )
        conn.close()

        with (
            patch(
                "job_finder.web.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True}),
            ),
            patch("job_finder.web.ats_scanner._probe.time.sleep"),
            patch(
                "job_finder.web.ats_detection.careers_url_is_live",
                return_value=True,  # URL is live → reject the pinpoint hit
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None
