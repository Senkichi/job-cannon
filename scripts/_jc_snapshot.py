#!/usr/bin/env python3
"""Read-only DB snapshot for overnight-run monitoring.

Prints a labeled set of metrics so before/after deltas can be diffed per job.
Opens the DB read-only (mode=ro URI) so it can never mutate the 865MB prod DB.

Usage:
    uv run python scripts/_jc_snapshot.py [LABEL]

DB path resolution: $JC_DB_PATH env, else the canonical main-repo jobs.db.
"""

import os
import sqlite3
import sys

def _default_db() -> str:
    """Resolve the jobs.db path portably: $JC_DB_PATH, else
    $JOB_CANNON_USER_DATA_DIR/jobs.db, else <repo-root>/jobs.db."""
    env = os.environ.get("JC_DB_PATH")
    if env:
        return env
    base = os.environ.get("JOB_CANNON_USER_DATA_DIR") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "jobs.db")


DB = _default_db()


def conn_ro() -> sqlite3.Connection:
    uri = f"file:{DB}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=30)
    return c


def q1(c, sql, params=()):
    """Single scalar."""
    try:
        row = c.execute(sql, params).fetchone()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        return f"ERR({type(e).__name__}: {e})"


def qrows(c, sql, params=()):
    try:
        return c.execute(sql, params).fetchall()
    except Exception as e:  # noqa: BLE001
        return [("ERR", f"{type(e).__name__}: {e}")]


def cols(c, table):
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    c = conn_ro()
    jc = cols(c, "jobs")

    print(f"==== DB SNAPSHOT [{label}] ====")
    print(f"db={DB}")

    print(f"total_jobs                = {q1(c, 'SELECT COUNT(*) FROM jobs')}")
    if "jd_full" in jc:
        _with = q1(c, "SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND jd_full != ''")
        _without = q1(c, "SELECT COUNT(*) FROM jobs WHERE jd_full IS NULL OR jd_full = ''")
        print(f"jobs_with_jd_full         = {_with}")
        print(f"jobs_missing_jd_full      = {_without}")
    if "classification" in jc and "jd_full" in jc:
        # Scoring backlog: has jd_full, not yet classified, not archived/dismissed.
        print(
            "scoring_backlog           = "
            + str(
                q1(
                    c,
                    "SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND jd_full != '' "
                    "AND classification IS NULL AND (pipeline_status IS NULL OR "
                    "pipeline_status NOT IN ('archived','dismissed'))",
                )
            )
        )
        print(
            "classification IS NULL    = "
            f"{q1(c, 'SELECT COUNT(*) FROM jobs WHERE classification IS NULL')}"
        )

    if "classification" in jc:
        print("-- by classification --")
        for k, v in qrows(
            c,
            "SELECT COALESCE(classification,'(null)'), COUNT(*) FROM jobs "
            "GROUP BY classification ORDER BY 2 DESC",
        ):
            print(f"   {str(k):<18} {v}")

    if "enrichment_tier" in jc:
        print("-- by enrichment_tier --")
        for k, v in qrows(
            c,
            "SELECT COALESCE(enrichment_tier,'(null)'), COUNT(*) FROM jobs "
            "GROUP BY enrichment_tier ORDER BY 2 DESC",
        ):
            print(f"   {str(k):<18} {v}")

    if "pipeline_status" in jc:
        print("-- by pipeline_status --")
        for k, v in qrows(
            c,
            "SELECT COALESCE(pipeline_status,'(null)'), COUNT(*) FROM jobs "
            "GROUP BY pipeline_status ORDER BY 2 DESC",
        ):
            print(f"   {str(k):<18} {v}")

    if "source" in jc:
        print("-- by source (top 15) --")
        for k, v in qrows(
            c,
            "SELECT COALESCE(source,'(null)'), COUNT(*) FROM jobs "
            "GROUP BY source ORDER BY 2 DESC LIMIT 15",
        ):
            print(f"   {str(k):<22} {v}")

    if "first_seen" in jc:
        print(
            "jobs_first_seen_today     = "
            + str(q1(c, "SELECT COUNT(*) FROM jobs WHERE date(first_seen)=date('now','localtime')"))
        )
        print(
            "jobs_first_seen_utc_today = "
            + str(q1(c, "SELECT COUNT(*) FROM jobs WHERE date(first_seen)=date('now')"))
        )
    if "is_stale" in jc:
        print(f"jobs_is_stale             = {q1(c, 'SELECT COUNT(*) FROM jobs WHERE is_stale=1')}")

    # companies
    if "companies" in {r[0] for r in qrows(c, "SELECT name FROM sqlite_master WHERE type='table'")}:
        print(f"total_companies           = {q1(c, 'SELECT COUNT(*) FROM companies')}")
        cc = cols(c, "companies")
        if "ats_platform" in cc:
            print("-- companies by ats_platform (top 12) --")
            for k, v in qrows(
                c,
                "SELECT COALESCE(ats_platform,'(null)'), COUNT(*) FROM companies "
                "GROUP BY ats_platform ORDER BY 2 DESC LIMIT 12",
            ):
                print(f"   {str(k):<18} {v}")

    # user_activity recency
    print("-- last user_activity per scheduled_* action --")
    for k, v in qrows(
        c,
        "SELECT action, MAX(occurred_at) FROM user_activity "
        "WHERE action LIKE 'scheduled_%' OR action='sync' GROUP BY action ORDER BY 2 DESC",
    ):
        print(f"   {str(k):<32} {v}")

    print("-- user_activity failures (last 24h) --")
    fails = qrows(
        c,
        "SELECT action, COUNT(*) FROM user_activity "
        "WHERE json_extract(metadata,'$.status')='failed' "
        "AND occurred_at >= datetime('now','-24 hours') GROUP BY action",
    )
    if fails:
        for k, v in fails:
            print(f"   {str(k):<32} {v}")
    else:
        print("   (none)")

    # costs today
    tables = {r[0] for r in qrows(c, "SELECT name FROM sqlite_master WHERE type='table'")}
    if "costs" in tables:
        cccols = cols(c, "costs")
        amt = "cost_usd" if "cost_usd" in cccols else ("amount" if "amount" in cccols else None)
        tcol = (
            "created_at"
            if "created_at" in cccols
            else ("occurred_at" if "occurred_at" in cccols else None)
        )
        if amt and tcol:
            print(
                "costs_today_usd           = "
                + str(
                    q1(
                        c,
                        f"SELECT ROUND(COALESCE(SUM({amt}),0),4) FROM costs "
                        f"WHERE date({tcol})=date('now')",
                    )
                )
            )
            print("-- costs today by provider --")
            pcol = "provider" if "provider" in cccols else None
            if pcol:
                for row in qrows(
                    c,
                    f"SELECT COALESCE({pcol},'(null)'), ROUND(SUM({amt}),4), COUNT(*) FROM costs "
                    f"WHERE date({tcol})=date('now') GROUP BY {pcol} ORDER BY 2 DESC",
                ):
                    prov, total, n = (list(row) + [None, None, None])[:3]
                    print(f"   {str(prov):<18} ${total}  n={n}")
    print(f"==== END SNAPSHOT [{label}] ====", flush=True)
    c.close()


if __name__ == "__main__":
    main()
