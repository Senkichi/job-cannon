"""Migration 111 — jd_content_version watermark (versioned jd-content re-sweep).

The storage half of the fail-closed jd-content contract, mirroring m110 (title)
and m100 (dedup). It seeds the watermark the standing re-sweep
(``_run_jd_content_resweep_if_stale`` in ``_post_hooks.py``) reads:

    ``jd_content_version`` seeded to ``'0'`` in ``schema_meta`` (created by m100).
    Seeding to 0 — below the live ``JD_CONTENT_VERSION`` (1) — arms the first
    re-sweep on next startup, which re-validates every stored ``jd_full`` under
    the current contract. Deterministic-REJECT bodies (Wikipedia / bot wall /
    listing index / 404 / expired / zero title-overlap) are cleared + quarantined
    + re-queued for enrichment so the scorer never sees the garbage and a clean
    body is re-fetched. On a fresh/empty DB the sweep finds nothing and simply
    stamps the watermark to 1.

No forensic column (unlike m110's ``raw_title``): the jd-content heal does not
*rewrite* a body in place — it CLEARS the wrong-page body and re-enriches, so the
recovered JD comes from a fresh fetch, not a reversal of an edit. The quarantine
reason code (``jd_full_offsite`` / ``jd_full_expired``) is the forensic record of
why the body was dropped.

Seeding is ``INSERT OR IGNORE`` keyed on the watermark, so re-running this
migration never clobbers a watermark the hook has already advanced (same
idempotency contract as m100/m110).

Frozen-in-time note (MI-4): the watermark key ``'jd_content_version'`` and the
seed value ``'0'`` are inlined here, not imported, so a future rename of the
constant cannot alter what this migration does to historical DBs.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=111,
    description="jd_content_version watermark (versioned jd-content re-sweep)",
    sql=[
        # schema_meta exists since m100. Seed 0 (below live version) to arm the
        # first re-sweep; OR IGNORE so an already-advanced watermark is preserved.
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('jd_content_version', '0')",
    ],
)
