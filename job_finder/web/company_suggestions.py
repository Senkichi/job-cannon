"""Suggested-companies query for the Companies page (WP6, release polish).

Surfaces the companies most frequent in the user's ingested feed that are
not yet tracked in the ``companies`` table — seed material for the ATS
watchlist, which otherwise starts empty for every new user.

Name-matching note: ``companies.name`` is normalized while ``jobs.company``
is raw, so the exclusion is done in Python with the same
``normalize_company`` the upsert path uses (SQL-side equality would both
over- and under-match; see the plan's adversarial notes). A duplicate
suggestion is cosmetic — ``upsert_company`` keeps tracking idempotent.
"""

import sqlite3

from job_finder.web.dedup_normalizer import normalize_company


def get_suggested_companies(conn: sqlite3.Connection, limit: int = 8) -> list[dict]:
    """Top untracked companies in the feed, ranked by good-fit then volume.

    Returns dicts with ``company`` (raw name as it appears on jobs),
    ``job_cnt``, and ``good_cnt`` (jobs classified apply/consider).
    """
    candidates = conn.execute(
        """SELECT j.company, COUNT(*) AS job_cnt,
                  SUM(CASE WHEN j.classification IN ('apply', 'consider')
                      THEN 1 ELSE 0 END) AS good_cnt
           FROM jobs j
           WHERE j.company_id IS NULL
             AND j.company != ''
           GROUP BY j.company
           ORDER BY good_cnt DESC, job_cnt DESC"""
    ).fetchall()

    tracked: set[str] = set()
    for row in conn.execute("SELECT name, name_raw FROM companies").fetchall():
        tracked.add(row[0])
        if row[1]:
            tracked.add(row[1])

    suggestions = []
    for company, job_cnt, good_cnt in candidates:
        if company in tracked or normalize_company(company) in tracked:
            continue
        suggestions.append({"company": company, "job_cnt": job_cnt, "good_cnt": good_cnt})
        if len(suggestions) >= limit:
            break
    return suggestions
