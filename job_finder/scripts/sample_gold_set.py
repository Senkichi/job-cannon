"""Gold-set stratified sampler (Phase 3).

Picks 33 dedup_keys pre-Phase-2 (3 anchors + 6 apply_high + 6 apply_mid +
6 consider + 4 reject + 8 cross-source) and writes a JSON manifest. After
Phase 2 ships, picks 7 additional rows from the new low_signal classification.
The labeling CLI consumes the manifest and walks unlabeled rows.

Usage:
    uv run python -m job_finder.scripts.sample_gold_set --phase pre_phase_2
    uv run python -m job_finder.scripts.sample_gold_set --phase low_signal \\
        --out .planning/gold_set_manifest_low_signal.json
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

# Forced anchor cases — see Phase 1 lit survey. These rows are added to the
# manifest unconditionally so the harness always has the same anchor labels
# regardless of what RANDOM() returns for the bucketed strata.
ANCHOR_DEDUP_KEYS: tuple[str, ...] = (
    "vera therapeutics|tmf manager, clinical qa",
    "latent (ca)|machine learning engineer",
    "google deepmind|research engineer, frontier safety mitigations, deepmind",
)

_COMPOSITE_EXPR = (
    "(json_extract(sub_scores_json,'$.title_fit')"
    " + json_extract(sub_scores_json,'$.location_fit')"
    " + json_extract(sub_scores_json,'$.comp_fit')"
    " + json_extract(sub_scores_json,'$.domain_match')"
    " + json_extract(sub_scores_json,'$.seniority_match')"
    " + json_extract(sub_scores_json,'$.skills_match'))"
)


def _not_in_clause(exclude_keys: list[str]) -> tuple[str, list[str]]:
    """Build a `dedup_key NOT IN (?, ?, ...)` clause that handles empty input.

    SQLite errors on `NOT IN ()`. When exclude_keys is empty, return a
    tautology so the calling query stays well-formed.
    """
    if not exclude_keys:
        return "1=1", []
    placeholders = ",".join("?" * len(exclude_keys))
    return f"dedup_key NOT IN ({placeholders})", list(exclude_keys)


def _sample_by_classification(
    conn: sqlite3.Connection,
    classification: str,
    limit: int,
    exclude_keys: list[str],
    composite_min: int | None = None,
    composite_max: int | None = None,
) -> list[str]:
    """Pick `limit` rows matching classification + optional composite range."""
    not_in_sql, not_in_params = _not_in_clause(exclude_keys)
    where = ["classification = ?", not_in_sql]
    params: list = [classification, *not_in_params]
    if composite_min is not None:
        where.append(f"{_COMPOSITE_EXPR} >= ?")
        params.append(composite_min)
    if composite_max is not None:
        where.append(f"{_COMPOSITE_EXPR} <= ?")
        params.append(composite_max)
    sql = "SELECT dedup_key FROM jobs WHERE " + " AND ".join(where) + " ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def _sample_by_source_pattern(
    conn: sqlite3.Connection,
    source_pattern: str,
    limit: int,
    exclude_keys: list[str],
) -> list[str]:
    """Pick `limit` rows whose sources LIKE the given pattern."""
    not_in_sql, not_in_params = _not_in_clause(exclude_keys)
    sql = (
        "SELECT dedup_key FROM jobs "
        "WHERE sources LIKE ? AND classification IS NOT NULL "
        f"AND {not_in_sql} "
        "ORDER BY RANDOM() LIMIT ?"
    )
    params = [source_pattern, *not_in_params, limit]
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def sample_pre_phase_2_strata(
    db_path: str,
    anchor_dedup_keys: list[str] | None = None,
) -> dict:
    """Sample the pre-Phase-2 gold-set strata.

    Default targets: 3 anchors + 6 apply_high (composite ≥24) + 6 apply_mid
    (18 ≤ composite ≤ 23) + 6 consider + 4 reject + 8 cross-source (2 each
    from {linkedin, glassdoor, dataforseo, Workday}). Total = 33.

    Args:
        db_path: Path to jobs.db.
        anchor_dedup_keys: Iterable of dedup_keys to force into the manifest.
            None ⇒ default ANCHOR_DEDUP_KEYS. Pass [] to skip anchors.

    Returns:
        {"dedup_keys": [...], "strata": {bucket: count, ...}, "phase": "pre_phase_2"}
    """
    if anchor_dedup_keys is None:
        anchor_dedup_keys = list(ANCHOR_DEDUP_KEYS)
    keys: list[str] = list(anchor_dedup_keys)
    strata: dict[str, int] = {"anchors": len(anchor_dedup_keys)}

    with closing(sqlite3.connect(db_path)) as conn:
        # apply_high: composite ≥ 24
        picked = _sample_by_classification(
            conn, "apply", limit=6, exclude_keys=keys, composite_min=24
        )
        keys.extend(picked)
        strata["apply_high"] = len(picked)

        # apply_mid: 18 ≤ composite ≤ 23
        picked = _sample_by_classification(
            conn,
            "apply",
            limit=6,
            exclude_keys=keys,
            composite_min=18,
            composite_max=23,
        )
        keys.extend(picked)
        strata["apply_mid"] = len(picked)

        # consider: any composite
        picked = _sample_by_classification(conn, "consider", limit=6, exclude_keys=keys)
        keys.extend(picked)
        strata["consider"] = len(picked)

        # reject: any composite
        picked = _sample_by_classification(conn, "reject", limit=4, exclude_keys=keys)
        keys.extend(picked)
        strata["reject"] = len(picked)

        # cross_source: 2 each from 4 source patterns
        cross_source_added = 0
        for pattern in ("%linkedin%", "%glassdoor%", "%dataforseo%", "%Workday%"):
            picked = _sample_by_source_pattern(conn, pattern, limit=2, exclude_keys=keys)
            keys.extend(picked)
            cross_source_added += len(picked)
        strata["cross_source"] = cross_source_added

    return {"dedup_keys": keys, "strata": strata, "phase": "pre_phase_2"}


def sample_low_signal_stratum(db_path: str, n: int = 7) -> list[str]:
    """Sample n unlabeled rows with classification='low_signal'. Run after Phase 2."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT dedup_key FROM jobs "
            "WHERE classification = 'low_signal' AND gold_classification IS NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    return [r[0] for r in rows]


def write_manifest(manifest: dict, path: str) -> None:
    """Serialize manifest dict to JSON at path, creating parent dirs as needed."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--out", default=".planning/gold_set_manifest.json")
    parser.add_argument(
        "--phase",
        choices=("pre_phase_2", "low_signal"),
        default="pre_phase_2",
    )
    args = parser.parse_args()

    if args.phase == "pre_phase_2":
        manifest = sample_pre_phase_2_strata(args.db)
    else:
        keys = sample_low_signal_stratum(args.db)
        manifest = {
            "dedup_keys": keys,
            "strata": {"low_signal": len(keys)},
            "phase": "low_signal",
        }
    write_manifest(manifest, args.out)
    print(
        f"Wrote {len(manifest['dedup_keys'])} dedup_keys to {args.out} "
        f"(strata: {manifest['strata']})"
    )


if __name__ == "__main__":
    main()
