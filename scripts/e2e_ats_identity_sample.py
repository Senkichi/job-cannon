"""E2E sample for ATS identity reconciliation (URL evidence → verify → hit).

Picks ``n`` scan-enabled companies with non-hit ATS status plus ``n`` with
existing ``hit`` status, runs :func:`~job_finder.web.ats_identity_reconcile.reconcile_company_ats`,
and prints a compact summary.

Usage::
    uv run --active python scripts/e2e_ats_identity_sample.py [db_path] [n]

``db_path`` (optional): pass first only if ending in ``.db`` / ``.sqlite`` /
``.sqlite3`` (or literal ``jobs.db``). Numeric first arg becomes cohort ``n``.

Exit code 0 unless config/DB unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from job_finder.config import load_config, resolve_config_path
from job_finder.web.db_migrate import run_migrations
from job_finder.web.ats_identity_reconcile import reconcile_company_ats
from job_finder.web.db_helpers import standalone_connection


def _resolve_db(custom: str | None) -> tuple[Path, dict]:
    cfg: dict = {}
    try:
        cfg = load_config(resolve_config_path())
    except Exception:
        pass
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return p, cfg
    raw = cfg.get("db", {}).get("path", "jobs.db")
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path, cfg


def _truncate(s: str | None, n: int = 36) -> str:
    if not s:
        return ""
    return s[:n] + ("..." if len(s) > n else "")


def main() -> int:
    argv = sys.argv[1:]
    db_arg = None
    if argv:
        cand = argv[0]
        sfx = Path(cand).suffix.lower()
        # First arg looks like DB path rather than cohort size
        if sfx in (".db", ".sqlite", ".sqlite3"):
            db_arg = argv.pop(0)
        elif cand.lower() == "jobs.db":
            db_arg = argv.pop(0)

    limit = 10
    if argv:
        try:
            limit = max(1, min(50, int(argv[0])))
            argv.pop(0)
        except ValueError:
            print(f"Ignoring non-integer cohort size {argv[0]!r}", file=sys.stderr)
    try:
        db_path, cfg = _resolve_db(db_arg)
    except Exception as exc:
        print("Config/DB resolve failed:", exc, file=sys.stderr)
        return 2

    if not db_path.is_file():
        print(f"SQLite file not found: {db_path}", file=sys.stderr)
        return 2

    run_migrations(str(db_path))
    with standalone_connection(str(db_path)) as conn:
        # Prefer non-hit companies that have ATS-looking URL evidence first.
        unident = conn.execute(
            """SELECT c.id, c.name, c.ats_probe_status,
                     SUM(CASE
                       WHEN j.source_urls IS NOT NULL
                        AND trim(j.source_urls) != ''
                        AND j.source_urls != '[]' THEN 1 ELSE 0 END) AS rows_with_urls
                FROM companies c
                LEFT JOIN jobs j ON j.company_id = c.id
               WHERE c.scan_enabled = 1
                 AND c.ats_probe_status IN ('pending', 'miss', 'error')
               GROUP BY c.id
               ORDER BY rows_with_urls DESC, c.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        hits = conn.execute(
            """SELECT id, name, ats_probe_status, ats_platform, COALESCE(ats_slug, '')
               FROM companies
              WHERE scan_enabled = 1
                AND ats_probe_status = 'hit'
              ORDER BY id DESC
              LIMIT ?""",
            (limit,),
        ).fetchall()

    print(f"=== ATS identity reconcile E2E sample (n={limit} each cohort) ===")
    print(f"DB: {db_path}")
    print()

    cohorts = [
        ("unidentified (pending/miss/error)", unident),
        ("already_identified (hit)", hits),
    ]

    grand: dict[str, int] = {}

    with standalone_connection(str(db_path)) as conn:
        for label, rows in cohorts:
            print(f"--- Cohort: {label} ({len(rows)} picked) ---")
            if not rows:
                print("(none)\n")
                continue
            for r in rows:
                cid = int(r["id"])
                outcome = reconcile_company_ats(
                    conn,
                    cid,
                    reason="e2e_ats_identity_sample",
                    config=cfg if cfg else None,
                )
                tag = str(outcome.get("outcome"))
                grand[tag] = grand.get(tag, 0) + 1
                slug_snip = str(outcome.get("slug") or "")
                if len(slug_snip) > 32:
                    slug_snip = slug_snip[:32] + "..."
                bits = []
                if outcome.get("unique_urls_seen") is not None:
                    bits.append(f"uniq_urls={outcome['unique_urls_seen']}")
                if outcome.get("contributing_jobs") is not None:
                    bits.append(f"contrib_jobs={outcome['contributing_jobs']}")
                meta = (" [" + "; ".join(bits) + "]") if bits else ""
                pair = ""
                if outcome.get("platform") or slug_snip:
                    pair = f" ({outcome.get('platform') or '?'}:{slug_snip or '?'})"
                print(
                    f"  id={cid} row_status={str(r['ats_probe_status']):>7} "
                    f"name={_truncate(r['name'])} -> {tag}{pair}{meta}"
                )
            print()

    print("--- Aggregate outcomes (both cohorts combined) ---")
    for k, v in sorted(grand.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {v:3d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
