"""Migration 90 — autoheal shadow state column.

Adds ``shadow_legacy_wins`` to ``source_health``: the consecutive count of
live-traffic comparisons where the legacy/generic extractor outperformed an
adopted override. Consumed by the Phase D shadow guard (D2/D4); the column
ships in D1 so the parallel D2/D3 chunks cannot collide on migration numbers.

Planned as m088 in the Phase D plan; renumbered to m090 because m088/m089
were taken by intervening merges (the plan's renumber contingency).
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=90,
    description="autoheal shadow state: shadow_legacy_wins column",
    sql=[
        "ALTER TABLE source_health ADD COLUMN shadow_legacy_wins INTEGER NOT NULL DEFAULT 0",
    ],
)
