"""Rolling per-source corpus of scrubbed parser inputs + output snapshots."""

from __future__ import annotations

import json
import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.sources._pii_scrub import scrub_text
from job_finder.web.autoheal import BASELINE_WINDOW

MAX_SAMPLES_PER_SOURCE = 50


def append_sample(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    raw_text: str,
    output_snapshot: dict,
    *,
    scrub_identifiers: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Scrub *raw_text*, insert one sample, evict oldest beyond the cap. Commits."""
    scrubbed = scrub_text(raw_text or "", scrub_identifiers)
    conn.execute(
        "INSERT INTO corpus_sample (source, surface, raw_text, output_json, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, surface, scrubbed, json.dumps(output_snapshot), utc_now_iso()),
    )
    conn.execute(
        """DELETE FROM corpus_sample
           WHERE source = ? AND id NOT IN (
               SELECT id FROM corpus_sample WHERE source = ?
               ORDER BY id DESC LIMIT ?
           )""",
        (source, source, MAX_SAMPLES_PER_SOURCE),
    )
    conn.commit()


def baseline_yield(conn: sqlite3.Connection, source: str) -> float:
    """Mean job_count over the last BASELINE_WINDOW samples that produced ≥1 job.

    Zero when the source has no positive history — which means the break rule
    cannot fire (a source that never produced jobs can't 'break').
    """
    rows = conn.execute(
        "SELECT output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC LIMIT ?",
        (source, BASELINE_WINDOW),
    ).fetchall()
    counts = []
    for r in rows:
        try:
            c = int(json.loads(r[0]).get("job_count", 0))
        except (ValueError, TypeError):
            c = 0
        if c > 0:
            counts.append(c)
    return round(sum(counts) / len(counts), 2) if counts else 0.0
