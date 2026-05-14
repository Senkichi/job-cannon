"""Adapter for find_careers_url callsite (Phase 36)."""

from __future__ import annotations

import sqlite3
from typing import Any

import requests

from evals.cascade_audit.adapters import rows_to_dicts


class FindCareersUrlAdapter:
    """Adapter for find_careers_url callsite."""

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample companies with homepage_url for find_careers_url."""
        cursor = conn.execute(
            """
            SELECT dedup_key, homepage_url
            FROM companies
            WHERE homepage_url IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        )
        return rows_to_dicts(cursor)

    def exercise(self, row: dict, provider: str, config: dict, conn: sqlite3.Connection) -> dict:
        """Exercise find_careers_url production code."""
        from job_finder.web.careers_scraper import find_careers_url

        result = find_careers_url(row["homepage_url"], conn=conn, config=config)
        return {"url": result}

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold reference."""
        metrics = {}

        # URL HTTP 200 check
        candidate_url = candidate.get("url") if isinstance(candidate, dict) else None
        if candidate_url:
            try:
                resp = requests.head(candidate_url, timeout=10, allow_redirects=True)
                metrics["url_http_200"] = resp.status_code == 200
            except Exception:
                metrics["url_http_200"] = False
        else:
            metrics["url_http_200"] = False

        # Same eTLD+1 check
        gold_url = gold.get("url") if isinstance(gold, dict) else None
        if gold_url and candidate_url:
            from urllib.parse import urlparse

            gold_domain = urlparse(gold_url).netloc
            candidate_domain = urlparse(candidate_url).netloc
            metrics["same_etld1"] = gold_domain == candidate_domain
        else:
            metrics["same_etld1"] = False

        # Career keyword presence
        career_keywords = ["career", "jobs", "join", "team", "openings"]
        if candidate_url:
            metrics["career_keyword_presence"] = any(
                kw in candidate_url.lower() for kw in career_keywords
            )
        else:
            metrics["career_keyword_presence"] = False

        return metrics
