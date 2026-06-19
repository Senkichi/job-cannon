"""P1.5 — trust-ranked, pair-atomic salary reconciliation in the upsert path (D-4).

Covers the reconciliation contract added in P1.5 (issue #381):

  * trust-ranked precedence (ats_structured > jd_regex > llm_extract > feed/email)
  * pair-atomic min/max selection (no per-field COALESCE Franken-pairs — the S2 bug)
  * jooble→Greenhouse AND Greenhouse→jooble both converge to the Greenhouse pair,
    with the Greenhouse direction exercised through the REAL ATS-scan path
    (`_upsert_one_ats_api_job`) so the deleted "first-seen wins" suppression is
    proven gone
  * equal-rank refresh (a Greenhouse re-scan with a corrected range wins)
  * llm_extract / jd_regex cannot overwrite an ats_structured pair (enrichment path)
  * legacy NULL-provenance rows are overwritable by anything
  * observations accumulate across sightings; the touch path appends no duplicate
  * implausible value → quarantine (canonical NULL + unresolved_reasons + retained
    observation) via the m106 heal
  * ParsedJob plumbing round-trip (source_meta → ParsedJob → DB → observation log)

Uses the session-scoped migrated-DB template (conftest `migrated_db`) so every
test runs against the real, fully-migrated schema (m106 columns present).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from job_finder.db import upsert_job
from job_finder.db._jobs import (
    _MAX_SALARY_OBSERVATIONS,
    _merge_salary_observations,
    _reconcile_salary_for_write,
    _reconcile_salary_pair_for_upsert,
)
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parsed(
    *,
    title: str,
    smin: int | None,
    smax: int | None,
    provenance: str | None,
    period: str = "annual",
    currency: str = "USD",
    raw_text: str | None = None,
) -> ParsedJob:
    """Build a ParsedJob through from_job + source_meta (the real capture path).

    ``salary_provenance`` / ``salary_observation`` ride in via source_meta exactly
    as the ATS scanner plumbs them post-P1.5.
    """
    obs = None
    if smin is not None or smax is not None:
        obs = {
            "min_value": smin,
            "max_value": smax,
            "period": period,
            "currency": currency,
            "provenance": provenance,
            "raw_text": raw_text,
        }
    job = Job(
        title=title,
        company="ReconCo",
        location="Remote",
        source="greenhouse" if provenance == "ats_structured" else "portal_jooble",
        source_url=f"https://example.com/{title.replace(' ', '-')}",
        description="x" * 300,
        salary_min=smin,
        salary_max=smax,
        salary_currency=currency,
        salary_period=period,
    )
    sm: dict = {}
    if provenance is not None:
        sm["salary_provenance"] = provenance
    if obs is not None:
        sm["salary_observation"] = obs
    parsed = ParsedJob.from_job(job, source_meta=sm)
    assert isinstance(parsed, ParsedJob)
    return parsed


def _read(conn: sqlite3.Connection, dedup_key: str) -> dict:
    row = conn.execute(
        "SELECT salary_min, salary_max, salary_period, salary_currency, "
        "salary_provenance, salary_observations, unresolved_reasons "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# Pure reconciler — pair-atomic trust ranking
# ---------------------------------------------------------------------------


class TestPairAtomicReconciler:
    def test_higher_rank_incoming_replaces_whole_tuple(self):
        cols, changed = _reconcile_salary_pair_for_upsert(
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
            3_000,
            251_000,
            "unknown",
            "USD",
            "feed_string",
        )
        assert changed is True
        assert cols == {
            "salary_min": 150_000,
            "salary_max": 200_000,
            "salary_period": "annual",
            "salary_currency": "USD",
            "salary_provenance": "ats_structured",
        }

    def test_lower_rank_incoming_frozen_out(self):
        cols, changed = _reconcile_salary_pair_for_upsert(
            3_000,
            251_000,
            "unknown",
            "USD",
            "feed_string",
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
        )
        assert changed is False
        assert cols == {}

    def test_equal_rank_incoming_refreshes(self):
        # Greenhouse re-scan with a corrected range replaces the prior Greenhouse pair.
        cols, changed = _reconcile_salary_pair_for_upsert(
            160_000,
            210_000,
            "annual",
            "USD",
            "ats_structured",
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
        )
        assert changed is True
        assert cols["salary_min"] == 160_000
        assert cols["salary_max"] == 210_000

    def test_equal_rank_same_values_no_canonical_change(self):
        # A same-value re-assertion within the same trust class is not an update.
        cols, changed = _reconcile_salary_pair_for_upsert(
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
        )
        assert changed is False
        # Tuple still offered (period/currency/provenance may differ) but the
        # canonical pair is unchanged.
        assert cols["salary_min"] == 150_000

    def test_null_stored_pair_fills_regardless_of_rank(self):
        cols, changed = _reconcile_salary_pair_for_upsert(
            120_000,
            150_000,
            "annual",
            "USD",
            "feed_string",
            None,
            None,
            None,
            None,
            None,
        )
        assert changed is True
        assert cols["salary_min"] == 120_000

    def test_null_incoming_never_clobbers(self):
        cols, changed = _reconcile_salary_pair_for_upsert(
            None,
            None,
            "unknown",
            "USD",
            "feed_string",
            150_000,
            200_000,
            "annual",
            "USD",
            "ats_structured",
        )
        assert changed is False
        assert cols == {}

    def test_legacy_null_provenance_overwritable_by_anything(self):
        # Stored pair has values but NULL provenance (legacy) → rank 0 → any
        # genuine writer wins.
        cols, changed = _reconcile_salary_pair_for_upsert(
            120_000,
            150_000,
            "annual",
            "USD",
            "feed_string",
            9_000,
            338_000,
            "unknown",
            "USD",
            None,
        )
        assert changed is True
        assert cols["salary_min"] == 120_000


# ---------------------------------------------------------------------------
# Observation log — accumulate, dedupe, cap
# ---------------------------------------------------------------------------


class TestObservationLog:
    def test_accumulates_distinct(self):
        a = {"provenance": "feed_string", "raw_text": "a", "min_value": 1, "max_value": 2}
        b = {"provenance": "ats_structured", "raw_text": "b", "min_value": 3, "max_value": 4}
        merged, changed = _merge_salary_observations([a], [b])
        assert changed is True
        assert merged == [a, b]

    def test_dedupes_identical(self):
        a = {"provenance": "feed_string", "raw_text": "a", "min_value": 1, "max_value": 2}
        merged, changed = _merge_salary_observations([a], [dict(a)])
        assert changed is False
        assert merged == [a]

    def test_caps_oldest_dropped(self):
        stored = [
            {"provenance": "feed_string", "raw_text": str(i), "min_value": i, "max_value": i}
            for i in range(_MAX_SALARY_OBSERVATIONS)
        ]
        incoming = [
            {"provenance": "ats_structured", "raw_text": "new", "min_value": 9, "max_value": 9}
        ]
        merged, changed = _merge_salary_observations(stored, incoming)
        assert changed is True
        assert len(merged) == _MAX_SALARY_OBSERVATIONS
        assert merged[-1]["raw_text"] == "new"
        assert merged[0]["raw_text"] == "1"  # oldest (index 0) dropped


# ---------------------------------------------------------------------------
# Enrichment reconciler — rank gate
# ---------------------------------------------------------------------------


class TestEnrichmentRankGate:
    def test_llm_extract_cannot_overwrite_ats_structured(self):
        cols, dropped = _reconcile_salary_for_write(
            100_000,
            120_000,
            150_000,
            200_000,
            incoming_provenance="llm_extract",
            stored_provenance="ats_structured",
        )
        assert dropped is True
        assert cols == {}

    def test_jd_regex_cannot_overwrite_ats_structured(self):
        cols, dropped = _reconcile_salary_for_write(
            100_000,
            120_000,
            150_000,
            200_000,
            incoming_provenance="jd_regex",
            stored_provenance="ats_structured",
        )
        assert dropped is True
        assert cols == {}

    def test_llm_extract_fills_null_stored(self):
        cols, dropped = _reconcile_salary_for_write(
            100_000,
            120_000,
            None,
            None,
            incoming_provenance="llm_extract",
            stored_provenance=None,
        )
        assert dropped is False
        assert cols == {"salary_min": 100_000, "salary_max": 120_000}

    def test_jd_regex_overwrites_llm_extract(self):
        # jd_regex (3) > llm_extract (2) → proceeds (no rank-drop).
        cols, dropped = _reconcile_salary_for_write(
            100_000,
            120_000,
            90_000,
            110_000,
            incoming_provenance="jd_regex",
            stored_provenance="llm_extract",
        )
        assert dropped is False
        assert cols == {"salary_min": 100_000, "salary_max": 120_000}


# ---------------------------------------------------------------------------
# DB integration — convergence through upsert_job
# ---------------------------------------------------------------------------


class TestUpsertConvergence:
    def test_jooble_then_greenhouse_converges_to_greenhouse(self, migrated_db):
        _, conn = migrated_db
        title = "Senior Data Scientist"
        # jooble feed-string guess lands first (the S2-shaped junk pair).
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=3_000,
                smax=251_000,
                provenance="feed_string",
                period="unknown",
                raw_text="$3K - $251K",
            ),
        )
        dk = f"reconco|{_norm(title)}"
        before = _read(conn, dk)
        assert before["salary_min"] == 3_000

        # Greenhouse structured pair arrives → must win.
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=170_000,
                smax=200_000,
                provenance="ats_structured",
                period="annual",
                raw_text="gh",
            ),
        )
        after = _read(conn, dk)
        assert (after["salary_min"], after["salary_max"]) == (170_000, 200_000)
        assert after["salary_provenance"] == "ats_structured"
        # Both observations retained.
        obs = json.loads(after["salary_observations"])
        provs = {o["provenance"] for o in obs}
        assert {"feed_string", "ats_structured"} <= provs

    def test_greenhouse_then_jooble_keeps_greenhouse(self, migrated_db):
        _, conn = migrated_db
        title = "Staff ML Engineer"
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=170_000,
                smax=200_000,
                provenance="ats_structured",
                period="annual",
                raw_text="gh",
            ),
        )
        # Lower-rank jooble re-sighting must NOT clobber the Greenhouse pair.
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=3_000,
                smax=251_000,
                provenance="feed_string",
                period="unknown",
                raw_text="$3K - $251K",
            ),
        )
        dk = f"reconco|{_norm(title)}"
        after = _read(conn, dk)
        assert (after["salary_min"], after["salary_max"]) == (170_000, 200_000)
        assert after["salary_provenance"] == "ats_structured"

    def test_touch_path_no_duplicate_observation(self, migrated_db):
        _, conn = migrated_db
        title = "Analytics Lead"
        p = _make_parsed(
            title=title,
            smin=120_000,
            smax=150_000,
            provenance="feed_string",
            period="annual",
            raw_text="$120K - $150K",
        )
        upsert_job(conn, p)
        dk = f"reconco|{_norm(title)}"
        first = json.loads(_read(conn, dk)["salary_observations"])
        # Re-ingest the identical sighting — observation must not duplicate.
        upsert_job(conn, p)
        second = json.loads(_read(conn, dk)["salary_observations"])
        assert first == second
        assert len(second) == 1

    def test_greenhouse_rescan_refreshes_equal_rank(self, migrated_db):
        _, conn = migrated_db
        title = "Principal Engineer"
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=150_000,
                smax=200_000,
                provenance="ats_structured",
                period="annual",
                raw_text="gh-v1",
            ),
        )
        # Corrected Greenhouse range, same trust class → refreshes.
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=160_000,
                smax=210_000,
                provenance="ats_structured",
                period="annual",
                raw_text="gh-v2",
            ),
        )
        dk = f"reconco|{_norm(title)}"
        after = _read(conn, dk)
        assert (after["salary_min"], after["salary_max"]) == (160_000, 210_000)


def _norm(title: str) -> str:
    """Mirror the dedup_key title-normalization for assertion convenience."""
    from job_finder.normalizers import normalize_title
    from job_finder.web.careers_crawler._title_filters import clean_title

    return normalize_title(clean_title(title))


# ---------------------------------------------------------------------------
# Greenhouse convergence through the REAL ATS-scan path (suppression deleted)
# ---------------------------------------------------------------------------


class TestAtsScanPathSuppressionDeleted:
    """The deleted "first-seen salary wins" suppression in _upsert_one_ats_api_job
    must no longer strip an incoming ATS salary when the row already has a pair.
    Exercised through the real scanner entry point, not a bare upsert_job call.
    """

    def test_ats_scan_salary_lands_over_existing_lower_rank(self, migrated_db):
        from job_finder.web.ats_scanner._run import _upsert_one_ats_api_job

        db_path, conn = migrated_db
        # Seed an existing low-rank (feed_string) salary via upsert.
        title = "Senior Data Scientist"
        upsert_job(
            conn,
            _make_parsed(
                title=title,
                smin=3_000,
                smax=251_000,
                provenance="feed_string",
                period="unknown",
                raw_text="$3K - $251K",
            ),
        )
        dk = f"reconco|{_norm(title)}"
        assert _read(conn, dk)["salary_min"] == 3_000

        # Now the ATS scanner sees the same job with a structured range. The
        # deleted suppression would have NULLed this incoming salary; the
        # reconciler must instead let ats_structured (4) overwrite feed_string (1).
        job_dict = {
            "title": title,
            "company_source": "Greenhouse",
            "source_url": "https://boards.greenhouse.io/reconco/jobs/1",
            "source_id": "gh-1",
            "location": "Remote",
            "salary_min": 170_000,
            "salary_max": 200_000,
            "salary_currency": "USD",
            "salary_period": "annual",
            "comp_json": json.dumps([{"min_cents": 17000000, "max_cents": 20000000}]),
            "description": "Drive analytics. " * 30,
        }
        summary: dict = {"jobs_new": 0, "errors": []}
        keys: list = []
        from job_finder.web.db_helpers import standalone_connection

        with standalone_connection(db_path) as scan_conn:
            _upsert_one_ats_api_job(conn, scan_conn, "ReconCo", job_dict, summary, keys)

        assert summary["errors"] == []
        after = _read(conn, dk)
        assert (after["salary_min"], after["salary_max"]) == (170_000, 200_000)
        assert after["salary_provenance"] == "ats_structured"


# ---------------------------------------------------------------------------
# ParsedJob plumbing round-trip
# ---------------------------------------------------------------------------


class TestParsedJobPlumbing:
    def test_source_meta_observation_seeds_log(self, migrated_db):
        _, conn = migrated_db
        title = "Data Engineer"
        p = _make_parsed(
            title=title,
            smin=140_000,
            smax=180_000,
            provenance="ats_structured",
            period="annual",
            raw_text="gh-de",
        )
        assert p.salary_provenance == "ats_structured"
        assert len(p.salary_observations) == 1
        upsert_job(conn, p)
        dk = f"reconco|{_norm(title)}"
        row = _read(conn, dk)
        obs = json.loads(row["salary_observations"])
        assert len(obs) == 1
        assert obs[0]["provenance"] == "ats_structured"
        assert obs[0]["raw_text"] == "gh-de"
        assert row["salary_provenance"] == "ats_structured"

    def test_no_salary_meta_leaves_provenance_null(self, migrated_db):
        _, conn = migrated_db
        title = "Product Manager"
        job = Job(
            title=title,
            company="ReconCo",
            location="Remote",
            source="portal_jooble",
            source_url="https://example.com/pm",
            description="y" * 300,
        )
        p = ParsedJob.from_job(job)
        assert isinstance(p, ParsedJob)
        assert p.salary_provenance is None
        assert p.salary_observations == []
        upsert_job(conn, p)
        dk = f"reconco|{_norm(title)}"
        row = _read(conn, dk)
        assert row["salary_provenance"] is None
        assert json.loads(row["salary_observations"]) == []


# ---------------------------------------------------------------------------
# m106 heal — salvage + quarantine on seeded corrupt rows
# ---------------------------------------------------------------------------


class TestM106Heal:
    def _seed_corrupt(
        self,
        conn: sqlite3.Connection,
        *,
        dedup_key: str,
        smin: int | None,
        smax: int | None,
        comp_json: str | None = None,
    ) -> None:
        # Direct INSERT to plant a corrupt row the live path would never write
        # (mimics a legacy row). I-01/I-02 triggers tolerate these magnitudes.
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, salary_min, "
            "salary_max, comp_data_json, scoring_provider, salary_observations, "
            "first_seen, last_seen) "
            "VALUES (?, ?, ?, '', ?, ?, ?, 'heuristic', '[]', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (dedup_key, "T", "C", smin, smax, comp_json),
        )
        conn.commit()

    def test_greenhouse_cents_salvaged(self, migrated_db):
        from job_finder.web.migrations.m106_salary_provenance import _heal
        from job_finder.web.migrations.types import MigrationContext

        db_path, conn = migrated_db
        # Northbeam-shaped: salary_min=17_000_000 (raw cents), comp_data_json holds
        # the verbatim pay range → ÷100 salvages to $170k–$200k.
        self._seed_corrupt(
            conn,
            dedup_key="cents|row",
            smin=17_000_000,
            smax=20_000_000,
            comp_json=json.dumps([{"min_cents": 17000000, "max_cents": 20000000}]),
        )
        _heal(MigrationContext(conn=conn, db_path=db_path, user_data_root="."))
        row = _read(conn, "cents|row")
        assert (row["salary_min"], row["salary_max"]) == (170_000, 200_000)
        assert row["salary_provenance"] == "ats_structured"
        # Observation retained.
        assert len(json.loads(row["salary_observations"])) == 1

    def test_subfloor_min_quarantined(self, migrated_db):
        from job_finder.web.migrations.m106_salary_provenance import (
            _SALARY_IMPLAUSIBLE_REASON,
            _heal,
        )
        from job_finder.web.migrations.types import MigrationContext

        db_path, conn = migrated_db
        # Trade Desk-shaped feed junk: 3k min, no corroborating structured evidence
        # → quarantine (canonical NULL, evidence retained, reason flagged).
        self._seed_corrupt(conn, dedup_key="junk|row", smin=3_000, smax=251_000)
        _heal(MigrationContext(conn=conn, db_path=db_path, user_data_root="."))
        row = _read(conn, "junk|row")
        assert row["salary_min"] is None
        assert row["salary_max"] is None
        reasons = json.loads(row["unresolved_reasons"])
        assert _SALARY_IMPLAUSIBLE_REASON in reasons
        # Evidence retained for /admin/review + healing.
        assert len(json.loads(row["salary_observations"])) == 1

    def test_heal_idempotent(self, migrated_db):
        from job_finder.web.migrations.m106_salary_provenance import _heal
        from job_finder.web.migrations.types import MigrationContext

        db_path, conn = migrated_db
        self._seed_corrupt(conn, dedup_key="junk|row2", smin=46, smax=46)
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=".")
        _heal(ctx)
        first = _read(conn, "junk|row2")
        _heal(ctx)  # second run is a no-op (row no longer matches candidate filter)
        second = _read(conn, "junk|row2")
        assert first["salary_min"] == second["salary_min"]
        # Observation log did not grow on the idempotent re-run.
        assert len(json.loads(first["salary_observations"])) == len(
            json.loads(second["salary_observations"])
        )

    def test_exit_criterion_no_oob_after_heal(self, migrated_db):
        from job_finder.web.migrations.m106_salary_provenance import _heal
        from job_finder.web.migrations.types import MigrationContext

        db_path, conn = migrated_db
        self._seed_corrupt(conn, dedup_key="oob|a", smin=17_000_000, smax=20_000_000)
        self._seed_corrupt(conn, dedup_key="oob|b", smin=3_000, smax=251_000)
        self._seed_corrupt(conn, dedup_key="oob|c", smin=46, smax=46)
        _heal(MigrationContext(conn=conn, db_path=db_path, user_data_root="."))
        oob = conn.execute(
            "SELECT count(*) FROM jobs WHERE salary_min > 5000000 "
            "OR (salary_min > 0 AND salary_min < 30000)"
        ).fetchone()[0]
        assert oob == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
