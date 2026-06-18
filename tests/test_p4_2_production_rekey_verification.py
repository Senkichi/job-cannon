"""P4.2 — production re-key verification fixtures (issue #378).

Verifies the full drain + merge contract that _run_rekey_if_stale executes on
first post-merge startup (version 1→2 mismatch triggers the re-key).

Design rules cited inline:
  D-8 — derived values are versioned; a standing idempotent re-derivation runs
         when the version changes; one-time sentinels are forbidden.

Scenario modelled on the §2-S5 EY duplicate pair measured in production
(2026-06-12):

  Row A (canonical — earliest first_seen = 2026-05-21):
    dedup_key  = "ey|de-data scientist-vg-w 4-cdao 0217"   (stale v1 form)
    location   = ""          (careers-crawl hardcoded empty — S4)
    jd_full    = "<full JD text>"  (enriched on first discovery)
    classification = "apply"       (scored before the normalizer bump)

  Row B (duplicate — later first_seen):
    dedup_key  = "ey|de-data scientist-vg-w4-cdao0217"     (also stale v1 form)
    location   = "Hyderabad"  (surfaced from URL slug / JSON-LD on a re-crawl)
    jd_full    = None          (not yet enriched)

After _run_rekey_if_stale (v1→v2):
  - Both rows collapse to the v2 canonical key.
  - merged row carries Hyderabad location (from Row B via _merge_locations).
  - merged row carries jd_full from Row A (canonical's jd_full is preserved;
    the duplicate had NULL so there is no better value to take — the update
    statement does not touch jd_full on the canonical).
  - earliest first_seen preserved (2026-05-21).
  - classification NULLed → queued for re-scoring (§2-S6 re-score).
  - merge_log carries merge_source = "rekey_v2".
  - Re-running is a no-op (idempotent, D-8).

Bookmark-breakage caveat (documented here, not a test assertion):
  dedup_key is embedded in URL routes (/jobs/<path:dedup_key>/...).  Re-keying
  changes the stored dedup_key for every row whose v1 key differs from its v2
  key.  Any browser bookmark or shared deep-link to the OLD key will 404 after
  the re-key.  This is acceptable for a single-user app; it is a known, one-time
  cost of fixing stale keys.  Merged "loser" rows are deleted entirely, so their
  old keys are gone.  The new canonical key is stable going forward unless
  NORMALIZER_VERSION is bumped again.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.normalizers import NORMALIZER_VERSION, derive_dedup_key
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import _post_hooks

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db():
    """Fully-migrated schema DB (temp file)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.unlink(path)


def _insert_job(conn, dedup_key, title, company, first_seen, **cols):
    """Insert a minimal job row into a fully-migrated DB."""
    base = {
        "location": "",
        "sources": "[]",
        "source_urls": "[]",
        "source_id": "",
        "pipeline_status": "discovered",
        "notes": "",
        "classification": None,
        "sub_scores_json": None,
        "fit_analysis": None,
        "jd_full": None,
        "locations_raw": None,
    }
    base.update(cols)
    keys = ["dedup_key", "title", "company", "first_seen", "last_seen", *base.keys()]
    vals = [dedup_key, title, company, first_seen, first_seen, *base.values()]
    placeholders = ",".join("?" * len(keys))
    conn.execute(f"INSERT INTO jobs ({','.join(keys)}) VALUES ({placeholders})", vals)
    conn.commit()


def _set_version_stale(conn, stored_version: int = 1) -> None:
    """Force the watermark to a stale version to arm the re-key hook."""
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'dedup_normalizer_version'",
        (str(stored_version),),
    )
    conn.commit()


def _stored_version(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'dedup_normalizer_version'"
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# EY duplicate-pair scenario (§2-S5 production archetype)
# ---------------------------------------------------------------------------

# The two v1 stale keys for the EY "DE-Data Scientist-VG-W4-CDAO0217" role.
# Under normalizer v1 (no digit<->letter separator), these keys were DISTINCT.
# Under normalizer v2 (#238 digit<->letter boundary), both derive to the same
# canonical key (the "W4" vs "W 4" gap collapses).
_EY_STALE_KEY_A = "ey|de-data scientist-vg-w 4-cdao 0217"  # space-separated form
_EY_STALE_KEY_B = "ey|de-data scientist-vg-w4-cdao0217"  # no-space form
_EY_TITLE = "DE-Data Scientist-VG-W4-CDAO0217"
_EY_COMPANY = "EY"
_EY_CANONICAL_KEY = derive_dedup_key(_EY_COMPANY, _EY_TITLE)

# Earliest first_seen across the two production rows.
_EY_FIRST_SEEN_CANONICAL = "2026-05-21T08:00:00"
_EY_FIRST_SEEN_DUPLICATE = "2026-05-23T14:00:00"


class TestEyPairMerge:
    """Verify the EY §2-S5 duplicate-pair merges with the correct field choices."""

    def _setup_ey_pair(self, conn):
        """Insert the two EY rows in their v1 stale-key form."""
        # Row A — canonical (earliest first_seen); has jd_full but empty location.
        # jd_full must be ≥ 200 chars to pass the I-13 content-density gate.
        _insert_job(
            conn,
            _EY_STALE_KEY_A,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_CANONICAL,
            location="",
            locations_raw="[]",
            jd_full=(
                "EY is seeking a senior data scientist to lead analytics initiatives "
                "within the CDAO organization. Responsibilities include developing "
                "machine learning models, driving data strategy, and collaborating "
                "with cross-functional teams to deliver business insights at scale."
            ),
            classification="apply",
            sub_scores_json='{"comp_fit": 4, "location_fit": 5}',
            fit_analysis="Strong match",
            pipeline_status="reviewing",
        )
        # Row B — duplicate (later first_seen); has Hyderabad from URL slug re-crawl,
        # but no jd_full yet.
        _insert_job(
            conn,
            _EY_STALE_KEY_B,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_DUPLICATE,
            location="Hyderabad",
            locations_raw='["Hyderabad"]',
            jd_full=None,
            classification="consider",
            sub_scores_json='{"comp_fit": 3, "location_fit": 2}',
            fit_analysis="Uncertain location",
        )

    def test_ey_pair_merges_to_single_canonical(self, migrated_db):
        """After re-key, the two EY rows collapse to one row under the v2 key."""
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        keys = {r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM jobs")}
        assert _EY_CANONICAL_KEY in keys, (
            f"Expected canonical key {_EY_CANONICAL_KEY!r} not found; got {keys}"
        )
        assert len(keys) == 1, f"Expected exactly 1 row; got keys: {keys}"

    def test_merged_row_keeps_earliest_first_seen(self, migrated_db):
        """Canonical row is chosen by earliest first_seen (2026-05-21)."""
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        row = conn.execute(
            "SELECT first_seen FROM jobs WHERE dedup_key = ?", (_EY_CANONICAL_KEY,)
        ).fetchone()
        assert row is not None
        assert row["first_seen"] == _EY_FIRST_SEEN_CANONICAL

    def test_merged_row_gets_hyderabad_location(self, migrated_db):
        """Hyderabad (from the duplicate row B) surfaces on the merged canonical.

        _merge_locations collects unique locations across all rows, Remote/Hybrid
        first, then others.  The canonical had location="" (empty — skipped as
        falsy) while the duplicate had location="Hyderabad".  The merged canonical
        therefore carries "Hyderabad" as its location string.
        """
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        row = conn.execute(
            "SELECT location FROM jobs WHERE dedup_key = ?", (_EY_CANONICAL_KEY,)
        ).fetchone()
        assert row is not None
        assert "Hyderabad" in row["location"]

    def test_merged_row_keeps_canonical_jd_full(self, migrated_db):
        """jd_full is preserved from the canonical (Row A).

        run_retroactive_dedup's UPDATE does not include jd_full, so the canonical
        row's jd_full is kept intact.  The duplicate had jd_full=NULL; the
        canonical had the enriched full body.  After merge, jd_full is the
        canonical's original value.
        """
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = ?", (_EY_CANONICAL_KEY,)
        ).fetchone()
        assert row is not None
        assert row["jd_full"] is not None
        assert "EY is seeking a senior data scientist" in row["jd_full"]

    def test_merged_row_classification_nulled_for_rescore(self, migrated_db):
        """classification is NULLed on merged canonicals (queued for re-scoring).

        The hook NULLs classification/sub_scores_json/fit_analysis on every
        canonical that was the target of a re-key merge (§2-S6: scores were
        computed on degraded inputs including bad location_fit; re-scoring after
        the merge picks up the correct location).
        """
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        row = conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis FROM jobs WHERE dedup_key = ?",
            (_EY_CANONICAL_KEY,),
        ).fetchone()
        assert row is not None
        assert row["classification"] is None, "classification must be NULLed for re-scoring"
        assert row["sub_scores_json"] is None, "sub_scores_json must be NULLed for re-scoring"
        assert row["fit_analysis"] is None, "fit_analysis must be NULLed for re-scoring"

    def test_merge_log_carries_rekey_v2_source(self, migrated_db):
        """merge_log rows written during re-key carry merge_source = 'rekey_v{N}'.

        This distinguishes re-key waves from the original once-ever migration
        (which used merge_source='migration') and from each other across future
        version bumps.
        """
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        expected_source = f"rekey_v{NORMALIZER_VERSION}"
        logs = conn.execute(
            "SELECT merge_source, canonical_key, merged_key FROM merge_log WHERE merge_source = ?",
            (expected_source,),
        ).fetchall()
        assert len(logs) >= 1, (
            f"Expected at least one merge_log row with merge_source={expected_source!r}"
        )
        assert any(r["canonical_key"] == _EY_CANONICAL_KEY for r in logs)

    def test_pipeline_status_highest_precedence_kept(self, migrated_db):
        """The higher-precedence pipeline_status is kept on the merged canonical.

        Row A had pipeline_status='reviewing' (rank 3); Row B had 'discovered'
        (rank 2).  After merge, 'reviewing' is kept.
        """
        _, conn = migrated_db
        self._setup_ey_pair(conn)
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (_EY_CANONICAL_KEY,)
        ).fetchone()
        assert row["pipeline_status"] == "reviewing"


# ---------------------------------------------------------------------------
# Idempotency contract (D-8: re-run is a no-op)
# ---------------------------------------------------------------------------


class TestRekeyIdempotency:
    """Re-running _run_rekey_if_stale after the version is stamped does nothing."""

    def test_second_run_after_ey_merge_is_noop(self, migrated_db):
        """Once the EY pair is merged and the watermark is stamped, a second
        invocation finds one row, no collisions, and adds zero merge_log entries.
        """
        _, conn = migrated_db
        # Insert EY pair and force stale.
        _insert_job(
            conn,
            _EY_STALE_KEY_A,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_CANONICAL,
        )
        _insert_job(
            conn,
            _EY_STALE_KEY_B,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_DUPLICATE,
        )
        _set_version_stale(conn)

        # First run: merges the pair and stamps the watermark.
        _post_hooks._run_rekey_if_stale(conn)
        merge_count_after_first = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]
        job_count_after_first = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        # Second run: watermark == NORMALIZER_VERSION → immediate no-op.
        _post_hooks._run_rekey_if_stale(conn)
        merge_count_after_second = conn.execute("SELECT COUNT(*) FROM merge_log").fetchone()[0]
        job_count_after_second = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        assert merge_count_after_second == merge_count_after_first, (
            "Second run must not add merge_log entries"
        )
        assert job_count_after_second == job_count_after_first, (
            "Second run must not change job count"
        )
        assert _stored_version(conn) == str(NORMALIZER_VERSION)

    def test_watermark_stamped_to_current_version_after_rekey(self, migrated_db):
        """After a successful re-key the watermark advances to NORMALIZER_VERSION."""
        _, conn = migrated_db
        _insert_job(
            conn,
            _EY_STALE_KEY_A,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_CANONICAL,
        )
        _insert_job(
            conn,
            _EY_STALE_KEY_B,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_DUPLICATE,
        )
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        assert _stored_version(conn) == str(NORMALIZER_VERSION)


# ---------------------------------------------------------------------------
# Duplicate-pair query contract: zero pairs after re-key
# ---------------------------------------------------------------------------


class TestDuplicatePairQueryZeroAfterRekey:
    """The §2 duplicate-pair query returns 0 after the re-key.

    "Recompute normalize_company(company)|normalize_title(title) for every row
    and count groups with >1 row; expect 0 after the re-key."
    """

    def _count_collision_groups(self, conn) -> int:
        """Return the number of (company, title) groups whose freshly-derived
        dedup_key collides with more than one stored row.

        This is the §2 duplicate-pair query formulated directly against the DB
        without relying on the stored dedup_key column.
        """
        rows = conn.execute("SELECT company, title, dedup_key FROM jobs").fetchall()
        key_counts: dict[str, int] = {}
        for row in rows:
            fresh_key = derive_dedup_key(row["company"], row["title"])
            key_counts[fresh_key] = key_counts.get(fresh_key, 0) + 1
        return sum(1 for cnt in key_counts.values() if cnt > 1)

    def test_ey_pair_produces_one_collision_group_before_rekey(self, migrated_db):
        """Sanity check: the EY pair registers as 1 collision group before re-keying."""
        _, conn = migrated_db
        _insert_job(
            conn,
            _EY_STALE_KEY_A,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_CANONICAL,
        )
        _insert_job(
            conn,
            _EY_STALE_KEY_B,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_DUPLICATE,
        )
        _set_version_stale(conn)

        assert self._count_collision_groups(conn) == 1

    def test_collision_group_count_zero_after_rekey(self, migrated_db):
        """After _run_rekey_if_stale, the §2 duplicate-pair query returns 0."""
        _, conn = migrated_db
        _insert_job(
            conn,
            _EY_STALE_KEY_A,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_CANONICAL,
        )
        _insert_job(
            conn,
            _EY_STALE_KEY_B,
            _EY_TITLE,
            _EY_COMPANY,
            _EY_FIRST_SEEN_DUPLICATE,
        )
        _set_version_stale(conn)

        _post_hooks._run_rekey_if_stale(conn)

        assert self._count_collision_groups(conn) == 0, (
            "Phase 4 exit criterion: duplicate-pair query must return 0 after re-key"
        )

    def test_multi_pair_backlog_all_zero_after_rekey(self, migrated_db):
        """Multiple stale-key collision groups (mirroring the 17 production pairs)
        all collapse to 0 collision groups after a single re-key pass.

        Uses a representative sample of the production §2-S5 backlog companies:
        EY, Netflix, OpenAI, Capital One — all affected by the #238 digit<->letter
        separator rule stranding keys.
        """
        _, conn = migrated_db
        pairs = [
            # (stale_key_a, stale_key_b, title, company, first_seen_a, first_seen_b)
            (
                "ey|de-data scientist-vg-w 4-cdao 0217",
                "ey|de-data scientist-vg-w4-cdao0217",
                "DE-Data Scientist-VG-W4-CDAO0217",
                "EY",
                "2026-05-21T08:00:00",
                "2026-05-23T14:00:00",
            ),
            (
                "netflix|l7 senior data scientist",
                "netflix|l 7 senior data scientist",
                "L7 Senior Data Scientist",
                "Netflix",
                "2026-04-01T00:00:00",
                "2026-04-02T00:00:00",
            ),
            (
                "openai|research engineer l5",
                "openai|research engineer l 5",
                "Research Engineer L5",
                "OpenAI",
                "2026-05-10T00:00:00",
                "2026-05-11T00:00:00",
            ),
            (
                "capital one|84data scientist",
                "capital one|84 data scientist",
                "84Data Scientist",
                "Capital One",
                "2026-03-15T00:00:00",
                "2026-03-16T00:00:00",
            ),
        ]
        for key_a, key_b, title, company, fs_a, fs_b in pairs:
            _insert_job(conn, key_a, title, company, fs_a)
            _insert_job(conn, key_b, title, company, fs_b)

        _set_version_stale(conn)
        assert self._count_collision_groups(conn) == len(pairs)

        _post_hooks._run_rekey_if_stale(conn)

        assert self._count_collision_groups(conn) == 0
        # One row per pair survives.
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == len(pairs)

    def test_distinct_jobs_survive_rekey_untouched(self, migrated_db):
        """Jobs that are genuinely distinct are never merged or deleted."""
        _, conn = migrated_db
        distinct_pairs = [
            ("acme|data scientist", "Data Scientist", "Acme", "2026-01-01T00:00:00"),
            ("acme|product manager", "Product Manager", "Acme", "2026-01-02T00:00:00"),
            ("google|software engineer", "Software Engineer", "Google", "2026-01-03T00:00:00"),
        ]
        for dk, title, company, fs in distinct_pairs:
            _insert_job(conn, dk, title, company, fs)

        _set_version_stale(conn)
        _post_hooks._run_rekey_if_stale(conn)

        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == len(distinct_pairs)
        assert self._count_collision_groups(conn) == 0
