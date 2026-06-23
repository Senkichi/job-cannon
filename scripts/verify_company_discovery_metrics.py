from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta

from job_finder.config import load_config

# High-quality job threshold (user definition), on the 6-30 composite scale below.
MIN_HQ_SCORE = 20

# Time windows: (days, label)
WINDOWS: list[tuple[int, str]] = [
    (7, "1 week"),
    (14, "2 weeks"),
    (30, "1 month"),
    (60, "2 months"),
]

# Match jobs.sources values case-insensitively (SQLite LOWER)
_ATS_SQL = "lower(je.value) IN ('ashby','greenhouse','lever','smartrecruiters','workday')"
_CAREERS_SQL = "je.value IN ('careers_crawl','careers_page')"

# v3.0 "high-quality" signal. The legacy jobs.score column was vestigial under
# v3.0 (never written by the scoring path — always 0) and dropped in m113, so the
# old COALESCE(j.score, 0) gate silently matched nothing. The real quality signal
# is the composite sum of the six 1-5 sub-scores (range 6-30). Mirrors
# job_finder.db._queries._SUB_SCORE_SUM_SQL, qualified to the ``j`` (jobs) alias.
_HQ_COMPOSITE_SQL = (
    "(COALESCE(json_extract(j.sub_scores_json, '$.title_fit'), 0) + "
    "COALESCE(json_extract(j.sub_scores_json, '$.location_fit'), 0) + "
    "COALESCE(json_extract(j.sub_scores_json, '$.comp_fit'), 0) + "
    "COALESCE(json_extract(j.sub_scores_json, '$.domain_match'), 0) + "
    "COALESCE(json_extract(j.sub_scores_json, '$.seniority_match'), 0) + "
    "COALESCE(json_extract(j.sub_scores_json, '$.skills_match'), 0))"
)


def _since_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def run_window_metrics(
    conn: sqlite3.Connection,
    days: int,
    label: str,
    since: str,
    ats_hit_companies: int,
    crawl_eligible_companies: int,
) -> None:
    print(f"\n{'=' * 60}", flush=True)
    print(f"Window: {label} ({days} days) — since {since}", flush=True)
    print(f"High-quality jobs: sub-score sum (of 30) >= {MIN_HQ_SCORE}", flush=True)
    print("=" * 60, flush=True)

    ats_ing = conn.execute(
        f"""
        SELECT COUNT(DISTINCT j.company_id) AS cnt
        FROM companies c
        JOIN jobs j ON j.company_id = c.id
        JOIN json_each(j.sources) je
        WHERE c.scan_enabled = 1
          AND c.ats_probe_status = 'hit'
          AND COALESCE(j.last_seen, j.first_seen) >= ?
          AND {_HQ_COMPOSITE_SQL} >= ?
          AND {_ATS_SQL}
        """,
        (since, MIN_HQ_SCORE),
    ).fetchone()
    ats_ing_count = ats_ing["cnt"]
    ats_ing_rate = (ats_ing_count / ats_hit_companies) * 100 if ats_hit_companies else 0.0

    ats_hq_possible = conn.execute(
        f"""
        SELECT COUNT(DISTINCT c.id) AS cnt
        FROM companies c
        JOIN jobs j ON j.company_id = c.id
        JOIN json_each(j.sources) je
        WHERE c.scan_enabled = 1
          AND COALESCE(j.last_seen, j.first_seen) >= ?
          AND {_HQ_COMPOSITE_SQL} >= ?
          AND {_ATS_SQL}
        """,
        (since, MIN_HQ_SCORE),
    ).fetchone()["cnt"]

    ats_hq_discovered = conn.execute(
        f"""
        SELECT COUNT(DISTINCT c.id) AS cnt
        FROM companies c
        JOIN jobs j ON j.company_id = c.id
        JOIN json_each(j.sources) je
        WHERE c.scan_enabled = 1
          AND c.ats_probe_status = 'hit'
          AND COALESCE(j.last_seen, j.first_seen) >= ?
          AND {_HQ_COMPOSITE_SQL} >= ?
          AND {_ATS_SQL}
        """,
        (since, MIN_HQ_SCORE),
    ).fetchone()["cnt"]

    ats_hq_rate = (ats_hq_discovered / ats_hq_possible) * 100 if ats_hq_possible else 0.0

    print(
        "\nATS ingestion (ATS-hit companies with HQ jobs from ATS sources in window):", flush=True
    )
    print(
        f"  {ats_ing_rate:.1f}% ({ats_ing_count} / {ats_hit_companies} ats_hit companies)",
        flush=True,
    )
    print(
        f"  ATS alignment (ATS-sourced HQ companies also ats_probe_status=hit): "
        f"{ats_hq_rate:.1f}% ({ats_hq_discovered} / {ats_hq_possible})",
        flush=True,
    )

    car_high_quality = conn.execute(
        f"""
        SELECT COUNT(DISTINCT j.company_id) AS cnt
        FROM companies c
        JOIN jobs j ON j.company_id = c.id
        JOIN json_each(j.sources) je
        WHERE c.scan_enabled = 1
          AND c.careers_url IS NOT NULL AND c.careers_url <> ''
          AND (c.ats_probe_status IS NULL OR c.ats_probe_status != 'hit')
          AND COALESCE(j.last_seen, j.first_seen) >= ?
          AND {_HQ_COMPOSITE_SQL} >= ?
          AND {_CAREERS_SQL}
        """,
        (since, MIN_HQ_SCORE),
    ).fetchone()["cnt"]

    car_ing_rate = (
        (car_high_quality / crawl_eligible_companies) * 100 if crawl_eligible_companies else 0.0
    )
    print(
        "\nCareers ingestion (eligible companies with HQ jobs from careers sources in window):",
        flush=True,
    )
    print(
        f"  {car_ing_rate:.1f}% "
        f"({car_high_quality} / {crawl_eligible_companies} crawl-eligible companies)",
        flush=True,
    )

    if days == 7:
        ats_spot = conn.execute(
            f"""
            SELECT c.id AS company_id, c.name_raw,
                   MAX(COALESCE(j.last_seen, j.first_seen)) AS newest_seen,
                   GROUP_CONCAT(DISTINCT je.value) AS job_sources
            FROM companies c
            JOIN jobs j ON j.company_id = c.id
            JOIN json_each(j.sources) je
            WHERE c.scan_enabled = 1 AND c.ats_probe_status = 'hit'
              AND {_HQ_COMPOSITE_SQL} >= ?
              AND {_ATS_SQL}
              AND COALESCE(j.last_seen, j.first_seen) >= ?
            GROUP BY c.id, c.name_raw
            ORDER BY newest_seen DESC
            LIMIT 5
            """,
            (MIN_HQ_SCORE, since),
        ).fetchall()
        print(f"\nSpot check (7d, ATS-hit, sub-score>={MIN_HQ_SCORE}, ATS-sourced):", flush=True)
        for r in ats_spot:
            print(
                f"  - {r['company_id']} {r['name_raw']} "
                f"newest={r['newest_seen']} sources={r['job_sources']}",
                flush=True,
            )

        spot = conn.execute(
            f"""
            SELECT c.id AS company_id, c.name_raw,
                   MAX(COALESCE(j.last_seen, j.first_seen)) AS newest_seen,
                   GROUP_CONCAT(DISTINCT je.value) AS job_sources
            FROM companies c
            JOIN jobs j ON j.company_id = c.id
            JOIN json_each(j.sources) je
            WHERE c.scan_enabled = 1
              AND c.careers_url IS NOT NULL AND c.careers_url <> ''
              AND (c.ats_probe_status IS NULL OR c.ats_probe_status != 'hit')
              AND {_HQ_COMPOSITE_SQL} >= ?
              AND {_CAREERS_SQL}
              AND COALESCE(j.last_seen, j.first_seen) >= ?
            GROUP BY c.id, c.name_raw
            ORDER BY newest_seen DESC
            LIMIT 5
            """,
            (MIN_HQ_SCORE, since),
        ).fetchall()
        print(
            f"\nSpot check (7d, careers-eligible, sub-score>={MIN_HQ_SCORE}, careers-sourced):",
            flush=True,
        )
        for r in spot:
            print(
                f"  - {r['company_id']} {r['name_raw']} "
                f"newest={r['newest_seen']} sources={r['job_sources']}",
                flush=True,
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="If set, only run this single window (days) instead of 7/14/30/60",
    )
    args = ap.parse_args()

    cfg = load_config()
    db_path = cfg["db"]["path"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    windows = [(args.days, f"{args.days} days")] if args.days is not None else WINDOWS

    print(f"DB: {db_path}", flush=True)
    print(f"High-quality jobs: sub-score sum (of 30) >= {MIN_HQ_SCORE}", flush=True)

    ats_dist = conn.execute(
        """
        SELECT ats_probe_status, scan_enabled, COUNT(*) AS cnt,
               SUM(CASE WHEN ats_slug IS NOT NULL THEN 1 ELSE 0 END) AS with_slug,
               SUM(CASE WHEN ats_probe_attempted_at IS NOT NULL THEN 1 ELSE 0 END) AS attempted
        FROM companies
        GROUP BY ats_probe_status, scan_enabled
        ORDER BY cnt DESC
        """
    ).fetchall()
    print("\nATS probe backlog (companies):", flush=True)
    for r in ats_dist[:20]:
        print(
            f"  status={r['ats_probe_status']!s} scan_enabled={r['scan_enabled']} "
            f"cnt={r['cnt']} with_slug={r['with_slug']} attempted={r['attempted']}",
            flush=True,
        )

    ats_disc = conn.execute(
        """
        SELECT SUM(CASE WHEN ats_probe_status='hit' THEN 1 ELSE 0 END) AS ats_hits,
               COUNT(*) AS ats_possible
        FROM companies
        WHERE scan_enabled = 1 AND ats_probe_attempted_at IS NOT NULL
        """
    ).fetchone()
    ats_disc_rate = (
        (ats_disc["ats_hits"] / ats_disc["ats_possible"]) * 100
        if ats_disc["ats_possible"]
        else 0.0
    )
    print("\nATS discovery rate (static, not windowed):", flush=True)
    print(
        f"  {ats_disc_rate:.1f}% (hit={ats_disc['ats_hits']} / probed={ats_disc['ats_possible']})",
        flush=True,
    )

    car_disc = conn.execute(
        """
        SELECT SUM(CASE WHEN careers_url IS NOT NULL AND careers_url <> '' THEN 1 ELSE 0 END) AS found,
               COUNT(*) AS possible
        FROM companies c
        WHERE c.scan_enabled = 1
          AND c.homepage_url IS NOT NULL AND c.homepage_url <> ''
          AND (c.ats_probe_status IS NULL OR c.ats_probe_status != 'hit')
        """
    ).fetchone()
    car_disc_rate = (
        (car_disc["found"] / car_disc["possible"]) * 100 if car_disc["possible"] else 0.0
    )
    print("\nCareers URL discovery (static, homepage -> careers_url, non-ATS-hit):", flush=True)
    print(
        f"  {car_disc_rate:.1f}% ({car_disc['found']} / {car_disc['possible']})",
        flush=True,
    )

    ats_hit_companies = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM companies c
        WHERE c.scan_enabled = 1 AND c.ats_probe_status = 'hit'
        """
    ).fetchone()["cnt"]

    crawl_eligible = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM companies c
        WHERE c.scan_enabled = 1
          AND c.careers_url IS NOT NULL AND c.careers_url <> ''
          AND (c.ats_probe_status IS NULL OR c.ats_probe_status != 'hit')
        """
    ).fetchone()["cnt"]

    print(
        "\nATS sources filter: lower(sources element) in "
        "(ashby, greenhouse, lever, smartrecruiters, workday)",
        flush=True,
    )
    print("Careers sources filter: careers_crawl | careers_page", flush=True)

    for days, label in windows:
        since = _since_iso(days)
        run_window_metrics(conn, days, label, since, ats_hit_companies, crawl_eligible)

    conn.close()


if __name__ == "__main__":
    main()
