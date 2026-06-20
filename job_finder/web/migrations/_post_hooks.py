"""Post-migration hooks — standing data fixups that ride on the migration loop.

These are NOT migrations. They are application-startup fixups that depend on
the migration chain having reached at least a certain version. The runner
(`run_migrations` in `db_migrate.py`) calls them after the loop terminates.

Putting them here keeps `db_migrate.py` focused on the migration runner.

The dedup re-key hook (`_run_rekey_if_stale`) is the standing, idempotent
re-derivation operation mandated by D-8: a derived value (dedup_key) records
the version of the function that produced it (`dedup_normalizer_version` in
`schema_meta`), and whenever the live `NORMALIZER_VERSION` differs, every row's
key is re-derived and duplicates are merged. This replaces the old once-ever
`merge_source='migration_complete'` sentinel, whose run-exactly-once gating let
#238's normalizer change strand 17 stale-key duplicate pairs.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.normalizers import NORMALIZER_VERSION

logger = logging.getLogger(__name__)

_VERSION_KEY = "dedup_normalizer_version"
_TITLE_VERSION_KEY = "title_hygiene_version"
_JD_CONTENT_VERSION_KEY = "jd_content_version"


def _read_meta_version(conn: sqlite3.Connection, key: str) -> int | None:
    """Return the integer watermark stored under *key* in ``schema_meta``, or None.

    None means the watermark cannot be read yet — ``schema_meta`` does not exist
    (DB is mid-migration, below m100) or *key* was never seeded. Callers treat
    None as "defer; not safe to decide". The single read primitive shared by the
    dedup / title-hygiene / jd-content re-sweeps (was duplicated per field).
    """
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _stamp_meta_version(conn: sqlite3.Connection, key: str, version: int) -> None:
    """Upsert the integer watermark *version* under *key* in ``schema_meta``."""
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(version)),
    )
    conn.commit()


def _read_stored_version(conn: sqlite3.Connection) -> int | None:
    """Stored ``dedup_normalizer_version`` watermark (or None — see _read_meta_version)."""
    return _read_meta_version(conn, _VERSION_KEY)


def _stamp_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the dedup_normalizer_version watermark (upsert)."""
    _stamp_meta_version(conn, _VERSION_KEY, version)


def _run_rekey_if_stale(conn: sqlite3.Connection) -> None:
    """Re-derive dedup_keys + merge duplicates when the normalizer version drifts.

    Standing, idempotent re-key operation (D-8). Compares the stored
    ``dedup_normalizer_version`` against the live ``NORMALIZER_VERSION``:

    - Versions equal → nothing owed; return immediately (the common startup
      path — one cheap SELECT).
    - Versions differ → run ``run_retroactive_dedup`` (logging merges as
      ``rekey_v{N}``), stamp the watermark to ``NORMALIZER_VERSION``, and NULL
      classification/sub_scores/fit_analysis on merged canonicals so the v3
      scorer re-derives them. Re-keyed singletons need no rescore (their facts
      are unchanged; only the key string moved).
    - Watermark unreadable (``schema_meta`` absent, DB below m100 mid-migration)
      → defer. m100 seeds the watermark, so the next startup decides correctly.
      This is the "keep honoring the old sentinel for fresh DBs mid-migration"
      contract: the legacy ``migration_complete`` sentinel still suppresses a
      redundant v1 dedup until m100 has run, after which the watermark governs.

    Args:
        conn: Open SQLite connection (must have m100 applied to act).
    """
    try:
        stored = _read_stored_version(conn)
        if stored is None:
            # schema_meta not present / unseeded — m100 hasn't run yet. Defer.
            return
        if stored == NORMALIZER_VERSION:
            return  # Keys already at the current version — nothing to do.

        from job_finder.web.dedup_normalizer import run_retroactive_dedup

        merge_source = f"rekey_v{NORMALIZER_VERSION}"
        merged_count = run_retroactive_dedup(conn, merge_source=merge_source)

        # Stamp the watermark FIRST so a crash after a partial merge still
        # records progress at the target version (the operation is idempotent —
        # a re-run finds no remaining collisions).
        _stamp_version(conn, NORMALIZER_VERSION)

        logger.info(
            "Dedup re-key v%d: merged %d duplicate jobs (was version %d).",
            NORMALIZER_VERSION,
            merged_count,
            stored,
        )

        if merged_count > 0:
            # Activity-feed entry so the user sees the merge count.
            try:
                from job_finder.json_utils import utc_now_iso

                conn.execute(
                    """
                    INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                    VALUES (?, 'dedup_rekey', ?, 0, 0)
                """,
                    (utc_now_iso(), merged_count),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log dedup re-key run: %s", e)

            # Queue merged canonical rows for re-scoring: NULL the v3 scoring
            # surface (classification/sub_scores_json) and the rationale
            # (fit_analysis). Only rows that were the canonical target of a
            # re-key merge are touched — re-keyed singletons keep their scores.
            try:
                canonical_keys = conn.execute(
                    "SELECT DISTINCT canonical_key FROM merge_log WHERE merge_source = ?",
                    (merge_source,),
                ).fetchall()
                for row in canonical_keys:
                    conn.execute(
                        """
                        UPDATE jobs
                           SET classification = NULL,
                               sub_scores_json = NULL,
                               fit_analysis = NULL
                         WHERE dedup_key = ?
                    """,
                        (row[0],),
                    )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to queue re-keyed rows for re-scoring: %s", e)

    except Exception as e:
        logger.warning("Dedup re-key failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Title-hygiene re-sweep (the retroactive half of the fail-closed title contract)
# ---------------------------------------------------------------------------


def _read_title_version(conn: sqlite3.Connection) -> int | None:
    """Stored ``title_hygiene_version`` watermark (or None — see _read_meta_version)."""
    return _read_meta_version(conn, _TITLE_VERSION_KEY)


def _stamp_title_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the title_hygiene_version watermark (upsert)."""
    _stamp_meta_version(conn, _TITLE_VERSION_KEY, version)


def _has_title_reason(reasons: list[str]) -> bool:
    """True if *reasons* carries any title-hygiene quarantine code."""
    from job_finder.web.careers_crawler._title_contract import TITLE_REASON_CODES

    return any(r in TITLE_REASON_CODES for r in reasons)


def _run_title_resweep_if_stale(conn: sqlite3.Connection) -> None:
    """Re-clean + re-validate every title when TITLE_HYGIENE_VERSION drifts (I-16/I-17).

    The retroactive counterpart to the ingest-time contract in
    ``ParsedJob.from_job``, and the title-side analogue of ``_run_rekey_if_stale``.
    This is what makes title hygiene NON-forward-only: legacy junk titles (which
    predate the contract and sit in the DB with empty ``unresolved_reasons``) get
    re-examined, and every future rule change heals the whole corpus by a single
    ``TITLE_HYGIENE_VERSION`` bump.

    Per row, under the current contract:
      * REPAIR — strip a trailing "<date> View Job ->" card tail via
        ``_strip_trailing_card_junk`` (the targeted repair — NOT full clean_title,
        whose legacy location strippers over-strip stored titles). If the title
        changes, preserve the pre-rewrite original in
        ``raw_title`` (once), then rewrite ``title``. This is the first sanctioned
        title rewriter; ``raw_title`` keeps the repair reversible.
      * RE-VALIDATE — recompute the I-16/I-17 reasons on the cleaned title (+ JD),
        preserving any non-title reasons already present.
      * RETRACT — a row that BECOMES quarantined (or whose title was repaired) and
        was already classified gets its scoring surface NULLed so the (now
        quarantine-gated) scorer leaves it off / re-evaluates it. This is what
        pulls the already-``apply`` Jobflarely cards off the board.

    After the row pass, ``run_retroactive_dedup`` re-keys + merges any duplicate
    collisions the title rewrites created (cleaned titles can collapse onto an
    existing clean row) — done HERE, not deferred to a future NORMALIZER_VERSION
    bump. The watermark is stamped LAST: the pass is idempotent (clean_title +
    the contract are deterministic), so a crash simply re-runs the whole pass.

    Versions equal → one cheap SELECT and return. Watermark unreadable (DB below
    m110) → defer. Never raises — observability/heals must not break startup.
    """
    try:
        from job_finder.normalizers import normalize_title
        from job_finder.web.careers_crawler._title_contract import (
            TITLE_HYGIENE_VERSION,
            TITLE_REASON_CODES,
            title_contract_violation,
        )

        # NB: the re-sweep repairs with _strip_trailing_card_junk (the targeted
        # date/CTA tail strip), NOT the full clean_title. clean_title's legacy
        # location strippers (_CITY_SUFFIX_RE / _NOSEP_TRAIL_LOC_RE) are tuned for
        # RAW scraped HTML at ingest; re-running them over already-stored titles
        # destructively over-strips legit qualifiers ("Junior-Mid Data Scientist"
        # -> "Junior", "Data Scientist (USA-Remote)" -> "Data Scientist (USA").
        # The dry-run gate (scripts/title_resweep_dryrun.py) caught this.
        from job_finder.web.careers_crawler._title_filters import _strip_trailing_card_junk

        stored = _read_title_version(conn)
        if stored is None:
            return  # m110 hasn't run yet — defer.
        if stored == TITLE_HYGIENE_VERSION:
            return  # Titles already validated at the current version.

        rows = conn.execute(
            "SELECT dedup_key, title, raw_title, unresolved_reasons, classification FROM jobs"
        ).fetchall()

        rewritten = quarantined = recleared = declassified = 0

        for dk, title, raw_title, ur_json, classification in rows:
            if title is None:
                continue

            new_title = _strip_trailing_card_junk(title)

            try:
                old_reasons = json.loads(ur_json) if ur_json else []
                if not isinstance(old_reasons, list):
                    old_reasons = []
            except (TypeError, ValueError):
                old_reasons = []

            # Recompute ONLY the title reasons; preserve every other reason.
            # (title_jd_mismatch is intentionally not applied here — see I-17 note
            # in parsed_job: it flags garbage JDs, not titles, and is deferred.)
            new_reasons = [r for r in old_reasons if r not in TITLE_REASON_CODES]
            shape_reason = title_contract_violation(new_title)
            if shape_reason is not None:
                new_reasons.append(shape_reason)

            title_changed = new_title != title
            reasons_changed = sorted(new_reasons) != sorted(old_reasons)
            if not title_changed and not reasons_changed:
                continue

            cols: list[str] = []
            params: list = []
            if title_changed:
                # Preserve the pre-rewrite original once (sparse: only changed rows).
                if raw_title is None:
                    cols.append("raw_title = ?")
                    params.append(title)
                cols.append("title = ?")
                params.append(new_title)
                rewritten += 1
            if reasons_changed:
                cols.append("unresolved_reasons = ?")
                params.append(json.dumps(new_reasons))

            became_quarantined = not _has_title_reason(old_reasons) and _has_title_reason(
                new_reasons
            )
            if became_quarantined:
                quarantined += 1
            elif _has_title_reason(old_reasons) and not _has_title_reason(new_reasons):
                recleared += 1

            # Retract from the board / queue for re-score when the row is now
            # quarantined OR its title changed SEMANTICALLY (normalized form
            # differs — a real repair like stripping a date/CTA tail). A purely
            # cosmetic change (trailing whitespace) leaves the normalized title
            # and the score valid, so it must NOT trigger a needless re-score.
            semantic_change = title_changed and normalize_title(new_title) != normalize_title(
                title
            )
            if (became_quarantined or semantic_change) and classification is not None:
                cols += [
                    "classification = NULL",
                    "sub_scores_json = NULL",
                    "fit_analysis = NULL",
                ]
                declassified += 1

            params.append(dk)
            conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE dedup_key = ?", params)

        conn.commit()

        # Title rewrites can collapse two rows onto the same dedup_key — re-key +
        # merge now (both rows already carry clean titles, so the canonical keeps
        # a clean one). Not deferred to a future NORMALIZER_VERSION bump.
        merged = 0
        if rewritten:
            from job_finder.web.dedup_normalizer import run_retroactive_dedup

            merge_source = f"title_resweep_v{TITLE_HYGIENE_VERSION}"
            merged = run_retroactive_dedup(conn, merge_source=merge_source)
            if merged:
                # Merged canonicals gained facts from their duplicates — re-score.
                try:
                    for (ckey,) in conn.execute(
                        "SELECT DISTINCT canonical_key FROM merge_log WHERE merge_source = ?",
                        (merge_source,),
                    ).fetchall():
                        conn.execute(
                            "UPDATE jobs SET classification = NULL, "
                            "sub_scores_json = NULL, fit_analysis = NULL WHERE dedup_key = ?",
                            (ckey,),
                        )
                    conn.commit()
                except Exception as e:
                    logger.warning("Title re-sweep: failed to queue merged rows: %s", e)

        # Stamp LAST (idempotent pass): a crash re-runs the whole sweep cleanly.
        _stamp_title_version(conn, TITLE_HYGIENE_VERSION)

        logger.info(
            "Title re-sweep v%d: rewrote %d, quarantined %d, recleared %d, "
            "declassified %d, merged %d (was version %s).",
            TITLE_HYGIENE_VERSION,
            rewritten,
            quarantined,
            recleared,
            declassified,
            merged,
            stored,
        )

        if rewritten or quarantined:
            try:
                from job_finder.json_utils import utc_now_iso

                conn.execute(
                    "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) "
                    "VALUES (?, 'title_resweep', ?, 0, 0)",
                    (utc_now_iso(), rewritten + quarantined),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log title re-sweep run: %s", e)

    except Exception as e:
        logger.warning("Title re-sweep failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# jd-content re-sweep (the retroactive half of the fail-closed jd-content contract)
# ---------------------------------------------------------------------------


def _run_jd_content_resweep_if_stale(conn: sqlite3.Connection) -> None:
    """Re-validate every stored jd_full when JD_CONTENT_VERSION drifts (I-18).

    The retroactive counterpart to the ingest-time contract in
    ``ParsedJob.from_job`` / the storage gate in ``set_jd_full``, and the
    jd-side analogue of ``_run_title_resweep_if_stale``. It is what makes the
    jd-content contract NON-forward-only: legacy garbage bodies (Wikipedia / bot
    walls / listing indexes / 404 / expired pages captured as the JD, all sitting
    in the DB with empty ``unresolved_reasons``) are re-examined, and every future
    rule change heals the whole corpus by a single ``JD_CONTENT_VERSION`` bump.

    HEAL = CLEAR + RE-QUEUE (not rewrite — a wrong page cannot be edited into a
    JD). Per row with a deterministic-REJECT body, under the current contract:
      * CLEAR the wrong-page ``jd_full`` (set NULL) so the scorer can never see it
        and the enrichment selector (``jd_full IS NULL``) re-fetches a real body.
      * RESET ``enrichment_tier`` to NULL so a row that had reached a terminal tier
        re-enters the fetch cascade. The content gate in ``set_jd_full`` now
        rejects a re-fetch that lands on the same junk, so the cascade falls
        through to a better tier instead of re-storing the garbage.
      * QUARANTINE: append the ``jd_full_offsite`` / ``jd_full_expired`` reason so
        the row surfaces on /admin/review with the reason it was dropped (the
        forensic record — the body itself is intentionally not retained).
      * RETRACT: NULL the scoring surface of an already-classified row so the
        (now jd-less, quarantine-gated) scorer stops surfacing it until a clean
        body is re-fetched.

    AMBIGUOUS bodies are deliberately NOT touched here — they are the background
    LLM adjudicator's job, which runs off the startup path. Only the deterministic
    high-precision REJECTs are healed synchronously, so the sweep stays fast.

    The watermark is stamped LAST: ``jd_content_reject`` is deterministic, so the
    pass is idempotent and a crash simply re-runs it. Versions equal → one cheap
    SELECT and return. Watermark unreadable (DB below m111) → defer. Never raises.
    """
    try:
        from job_finder.db._jd_content_contract import (
            JD_CONTENT_REASON_CODES,
            JD_CONTENT_VERSION,
            jd_content_reject,
        )

        stored = _read_meta_version(conn, _JD_CONTENT_VERSION_KEY)
        if stored is None:
            return  # m111 hasn't run yet — defer.
        if stored == JD_CONTENT_VERSION:
            return  # jd_full already validated at the current version.

        rows = conn.execute(
            "SELECT dedup_key, title, jd_full, unresolved_reasons, classification "
            "FROM jobs WHERE jd_full IS NOT NULL AND TRIM(jd_full) != ''"
        ).fetchall()

        cleared = recleared = declassified = 0

        for dk, title, jd_full, ur_json, classification in rows:
            try:
                old_reasons = json.loads(ur_json) if ur_json else []
                if not isinstance(old_reasons, list):
                    old_reasons = []
            except (TypeError, ValueError):
                old_reasons = []

            rej = jd_content_reject(jd_full, title)
            # Recompute ONLY jd-content reasons; preserve every other reason.
            new_reasons = [r for r in old_reasons if r not in JD_CONTENT_REASON_CODES]
            if rej is not None:
                new_reasons.append(rej[0])

            had = any(r in JD_CONTENT_REASON_CODES for r in old_reasons)
            has = rej is not None
            reasons_changed = sorted(new_reasons) != sorted(old_reasons)
            if not has and not reasons_changed:
                continue  # clean and stays clean — nothing to do.

            cols: list[str] = []
            params: list = []
            if reasons_changed:
                cols.append("unresolved_reasons = ?")
                params.append(json.dumps(new_reasons))

            if has:
                cols += ["jd_full = NULL", "enrichment_tier = NULL"]
                if classification is not None:
                    cols += [
                        "classification = NULL",
                        "sub_scores_json = NULL",
                        "fit_analysis = NULL",
                    ]
                    declassified += 1
                cleared += 1
            elif had and not has:
                recleared += 1

            params.append(dk)
            conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE dedup_key = ?", params)

        conn.commit()

        # Stamp LAST (idempotent pass): a crash re-runs the whole sweep cleanly.
        _stamp_meta_version(conn, _JD_CONTENT_VERSION_KEY, JD_CONTENT_VERSION)

        logger.info(
            "jd-content re-sweep v%d: cleared %d, recleared %d, declassified %d (was version %s).",
            JD_CONTENT_VERSION,
            cleared,
            recleared,
            declassified,
            stored,
        )

        if cleared:
            try:
                from job_finder.json_utils import utc_now_iso

                conn.execute(
                    "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) "
                    "VALUES (?, 'jd_content_resweep', ?, 0, 0)",
                    (utc_now_iso(), cleared),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log jd-content re-sweep run: %s", e)

    except Exception as e:
        logger.warning("jd-content re-sweep failed (non-fatal): %s", e)
