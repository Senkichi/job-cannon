"""Suggested-companies query for the Companies page (WP6, release polish).

Surfaces the companies most frequent in the user's ingested feed that are
not yet tracked in the ``companies`` table — seed material for the ATS
watchlist, which otherwise starts empty for every new user.

Name-matching note: ``companies.name`` is normalized while ``jobs.company``
is raw, so the exclusion is done in Python with the same
``normalize_company`` the upsert path uses (SQL-side equality would both
over- and under-match; see the plan's adversarial notes). A duplicate
suggestion is cosmetic — ``upsert_company`` keeps tracking idempotent.

Cold-start fallback (Issue #660): when the user has no owner history
(good_cnt is zero/meaningless), rank candidates by profile match and
ATS scannability instead. This enables day-1 watchlist seeding for users
who declare no target companies.
"""

import sqlite3
from typing import TYPE_CHECKING

from job_finder.web.dedup_normalizer import normalize_company

if TYPE_CHECKING:
    from job_finder.web.user_data_dirs import ExperienceProfile


def get_suggested_companies(
    conn: sqlite3.Connection,
    limit: int = 8,
    profile: "ExperienceProfile | None" = None,
) -> list[dict]:
    """Top untracked companies in the feed, ranked by good-fit then volume.

    Cold-start fallback (Issue #660): when owner history is empty
    (no jobs with classification 'apply' or 'consider'), rank by
    profile match and ATS scannability instead.

    Args:
        conn: SQLite connection.
        limit: Maximum number of suggestions to return.
        profile: User's experience profile for cold-start ranking.
            If None, cold-start fallback is disabled.

    Returns dicts with ``company`` (raw name as it appears on jobs),
    ``job_cnt``, and ``good_cnt`` (jobs classified apply/consider).
    For cold-start results, also includes ``relevance_score`` and
    ``ats_boost``.
    """
    # Build tracked set for exclusion
    tracked: set[str] = set()
    for row in conn.execute("SELECT name, name_raw FROM companies").fetchall():
        tracked.add(row[0])
        if row[1]:
            tracked.add(row[1])

    # Check if we have owner history (any apply/consider classifications)
    has_history = conn.execute(
        """SELECT 1 FROM jobs
           WHERE classification IN ('apply', 'consider')
           LIMIT 1"""
    ).fetchone() is not None

    if has_history:
        # Standard ranking: good_cnt DESC, job_cnt DESC
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

        suggestions = []
        for company, job_cnt, good_cnt in candidates:
            if company in tracked or normalize_company(company) in tracked:
                continue
            suggestions.append({"company": company, "job_cnt": job_cnt, "good_cnt": good_cnt})
            if len(suggestions) >= limit:
                break
        return suggestions
    elif profile:
        # Cold-start fallback: rank by profile match and ATS scannability
        return _cold_start_suggestions(conn, tracked, limit, profile)
    else:
        # No history and no profile: return empty (cannot rank meaningfully)
        return []


def _cold_start_suggestions(
    conn: sqlite3.Connection,
    tracked: set[str],
    limit: int,
    profile: "ExperienceProfile",
) -> list[dict]:
    """Cold-start ranking: profile match + ATS scannability (Issue #660).

    Ranks companies by:
    1. Relevance score: title match + location match + skills match
    2. ATS boost: companies with known ATS platforms (Lever, Greenhouse, Ashby)
    3. Job count (tiebreaker)

    Args:
        conn: SQLite connection.
        tracked: Set of already-tracked company names (normalized + raw).
        limit: Maximum number of suggestions to return.
        profile: User's experience profile.

    Returns list of suggestion dicts with relevance_score and ats_boost.
    """
    target_titles = profile.get("target_titles", [])
    target_locations = profile.get("target_locations", [])
    skills = profile.get("skills", [])

    # Build candidate companies with their job counts
    candidates = conn.execute(
        """SELECT j.company, COUNT(*) AS job_cnt
           FROM jobs j
           WHERE j.company_id IS NULL
             AND j.company != ''
           GROUP BY j.company"""
    ).fetchall()

    # Score each candidate
    scored_candidates = []
    for company, job_cnt in candidates:
        if company in tracked or normalize_company(company) in tracked:
            continue

        # Fetch jobs for this company to calculate relevance
        jobs = conn.execute(
            """SELECT title, location, description
               FROM jobs
               WHERE company = ? AND company_id IS NULL""",
            (company,),
        ).fetchall()

        relevance_score = 0
        for title, location, description in jobs:
            # Title match: check if any target title appears in job title
            title_lower = title.lower() if title else ""
            for tt in target_titles:
                if tt.lower() in title_lower:
                    relevance_score += 1
                    break

            # Location match: check if any target location appears in job location
            location_lower = location.lower() if location else ""
            for tl in target_locations:
                if tl.lower() in location_lower:
                    relevance_score += 1
                    break

            # Skills match: check if any skill appears in description
            desc_lower = description.lower() if description else ""
            for skill in skills:
                if skill.lower() in desc_lower:
                    relevance_score += 0.5  # Partial weight for skills
                    break

        # ATS boost: check if company has known ATS platform
        ats_boost = 0
        company_row = conn.execute(
            "SELECT ats_platform FROM companies WHERE name = ? OR name_raw = ?",
            (normalize_company(company), company),
        ).fetchone()
        if company_row and company_row[0]:
            # Boost for known ATS platforms (product's core edge: day-of latency)
            ats_boost = 5

        scored_candidates.append(
            {
                "company": company,
                "job_cnt": job_cnt,
                "good_cnt": 0,  # No owner history in cold-start
                "relevance_score": relevance_score,
                "ats_boost": ats_boost,
            }
        )

    # Sort by: (relevance_score + ats_boost) DESC, job_cnt DESC
    scored_candidates.sort(
        key=lambda x: (x["relevance_score"] + x["ats_boost"], x["job_cnt"]),
        reverse=True,
    )

    return scored_candidates[:limit]
