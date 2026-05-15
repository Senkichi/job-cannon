"""Adapter for extract_jobs callsite (Phase 36)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import requests

from evals.cascade_audit.adapters import rows_to_dicts
from evals.cascade_audit.corpus_loader import _safe_cache_stem


class ExtractJobsAdapter:
    """Adapter for extract_jobs callsite."""

    def __init__(self, artifact_dir: Path) -> None:
        """Initialize with artifact directory for cached HTML."""
        self._artifact_dir = Path(artifact_dir)

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample companies with homepage_url for extract_jobs."""
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
        """Exercise extract_jobs production code."""
        from job_finder.web.careers_scraper import _extract_jobs_with_low_tier

        # Load cached HTML from artifacts/round_1/html/. Lazy-fetch on first
        # access so all providers in the same round share the same frozen
        # snapshot (#phase-36 audit followup: spec said HTML should be cached
        # at Round 1 start, but no fetcher step exists in the harness).
        dedup_key = row["dedup_key"]
        html_dir = self._artifact_dir / "round_1" / "html"
        html_dir.mkdir(parents=True, exist_ok=True)
        html_path = html_dir / f"{_safe_cache_stem(dedup_key)}.html"
        if not html_path.exists():
            resp = requests.get(
                row["homepage_url"],
                timeout=15,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (cascade-audit/phase-36)"},
            )
            resp.raise_for_status()
            html_path.write_text(resp.text, encoding="utf-8")

        cached_html = html_path.read_text(encoding="utf-8")

        result = _extract_jobs_with_low_tier(
            careers_url=row["homepage_url"],
            careers_html=cached_html,
            target_titles=config.get("target_titles", []),
            exclusions=config.get("exclusions", []),
            conn=conn,
            config=config,
        )
        return result

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold reference."""
        metrics = {}

        # Schema validation
        schema_valid = isinstance(candidate, list)
        metrics["schema_valid"] = schema_valid

        if not schema_valid:
            return metrics

        # URL HTTP 200 rate
        import requests

        urls_ok = 0
        total_urls = 0
        for job in candidate:
            if isinstance(job, dict) and "url" in job:
                total_urls += 1
                try:
                    resp = requests.head(job["url"], timeout=10, allow_redirects=True)
                    if resp.status_code == 200:
                        urls_ok += 1
                except Exception:
                    pass
        metrics["url_http_200_rate"] = urls_ok / total_urls if total_urls > 0 else 0.0

        # Title set Jaccard similarity
        gold_titles = set()
        candidate_titles = set()
        if isinstance(gold, list):
            gold_titles = {job.get("title", "").lower() for job in gold if isinstance(job, dict)}
        candidate_titles = {job.get("title", "").lower() for job in candidate if isinstance(job, dict)}

        if gold_titles and candidate_titles:
            intersection = gold_titles & candidate_titles
            union = gold_titles | candidate_titles
            metrics["title_set_jaccard"] = len(intersection) / len(union) if union else 0.0
        else:
            metrics["title_set_jaccard"] = 0.0

        # Hallucinated job rate
        if gold_titles:
            hallucinated = len(candidate_titles - gold_titles) / len(candidate_titles) if candidate_titles else 0.0
            metrics["hallucinated_job_rate"] = hallucinated
        else:
            metrics["hallucinated_job_rate"] = 0.0

        return metrics
