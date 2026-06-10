"""Migration 91 — autoheal careers re-key: drop stale global 'careers' rows.

Phase D / D3 re-keys careers capture per company (``careers:{hostname}``).
The old global ``careers`` rows aggregated every company into one corpus and
one health row — their blended baseline is misleading once keying is
per-company, so they are deleted. New captures repopulate per-company rows
organically on the next crawl.

Planned as m089 in the Phase D plan; renumbered to m091 because m088/m089
were taken by intervening merges (the plan's renumber contingency — D1 took
m090 for the same reason).
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=91,
    description="autoheal careers re-key: drop stale global 'careers' rows",
    sql=[
        "DELETE FROM corpus_sample WHERE source = 'careers'",
        "DELETE FROM source_health WHERE source = 'careers'",
    ],
)
