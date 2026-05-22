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

from job_finder.web.ats_detection import probe_hit_consistent_with_careers_url
from job_finder.web.db_migrate import run_migrations

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
        """Lever URL + speculative pinpoint hit → company ends on miss."""
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
