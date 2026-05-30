"""Corpus loader for cascade audit (Phase 36).

Samples production DB rows for the 6 non-scoring callsites and persists
dedup_keys for reproducibility across rounds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_cache_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    digest = hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    if not cleaned:
        cleaned = "item"
    return f"{cleaned[:80]}-{digest}"


class CorpusLoader:
    """Loads and caches corpus data for cascade audit evaluation.

    Samples production DB rows at Round 0 start, persists dedup_keys for
    reproducibility, and caches inputs (HTML, JD text) to artifacts/
    for deterministic execution across rounds.
    """

    def __init__(self, artifact_dir: Path, db_path: str) -> None:
        """Initialize corpus loader.

        Args:
            artifact_dir: Base directory for artifact storage.
            db_path: Path to production SQLite database.
        """
        self._artifact_dir = Path(artifact_dir)
        self._db_path = db_path
        self._dedup_keys_file = self._artifact_dir / "round_0" / "dedup_keys.json"

    def load_round_0(
        self,
        n_per_callsite: int,
        conn: sqlite3.Connection,
        judge_n_per_callsite: int = 10,
    ) -> dict[str, list[dict]]:
        """Sample corpus for all 6 callsites and cache inputs.

        Args:
            n_per_callsite: Number of rows to sample per callsite.
            conn: Open SQLite connection to production DB.
            judge_n_per_callsite: Sample size for judge-based callsites
                (description_reformat, company_research). Defaults to 10 so the
                calibration log has enough verdicts; persisted at Round 0 so
                Round 1+ can reload them deterministically.

        Returns:
            Dict mapping callsite name to list of sampled rows.
        """
        corpus: dict[str, list[dict]] = {}

        # Create round_0 artifact directories
        round_0_dir = self._artifact_dir / "round_0"
        round_0_dir.mkdir(parents=True, exist_ok=True)
        (round_0_dir / "html").mkdir(exist_ok=True)
        (round_0_dir / "recipes").mkdir(exist_ok=True)

        # Sample each callsite
        corpus["parse_structured_fields"] = self._sample_parse_structured_fields(
            n_per_callsite, conn, round_0_dir
        )
        corpus["find_careers_url"] = self._sample_find_careers_url(
            n_per_callsite, conn, round_0_dir
        )
        corpus["extract_jobs"] = self._sample_extract_jobs(
            50,
            conn,
            round_0_dir,  # Spec requires 50 companies for extract_jobs
        )
        corpus["description_reformat"] = self._sample_description_reformat(
            judge_n_per_callsite, conn, round_0_dir
        )
        corpus["company_research"] = self._sample_company_research(
            judge_n_per_callsite, conn, round_0_dir
        )
        corpus["ai_nav_discovery"] = self._sample_ai_nav_discovery(
            n_per_callsite, conn, round_0_dir
        )

        # Persist dedup_keys for reproducibility
        dedup_keys = {
            callsite: [row["dedup_key"] for row in rows] for callsite, rows in corpus.items()
        }
        self._dedup_keys_file.write_text(json.dumps(dedup_keys, indent=2), encoding="utf-8")

        return corpus

    def load_round_1(self, conn: sqlite3.Connection) -> dict[str, list[dict]]:
        """Load corpus using persisted dedup_keys from Round 0.

        All 6 callsites reload by their Round 0 keys — judge-based callsites
        included — so providers in Rounds 1+ see the same inputs they saw in
        Round 0. The calibration sample size for judge callsites is fixed at
        Round 0 (see ``judge_n_per_callsite`` on :meth:`load_round_0`).

        Args:
            conn: Open SQLite connection to production DB.

        Returns:
            Dict mapping callsite name to list of rows from production DB.
        """
        if not self._dedup_keys_file.exists():
            raise FileNotFoundError(
                f"dedup_keys.json not found at {self._dedup_keys_file}. "
                "Run Round 0 first to generate corpus."
            )

        dedup_keys = json.loads(self._dedup_keys_file.read_text(encoding="utf-8"))
        corpus: dict[str, list[dict]] = {}

        corpus["parse_structured_fields"] = self._load_by_keys(
            "jobs", dedup_keys["parse_structured_fields"], conn
        )
        corpus["find_careers_url"] = self._load_by_keys(
            "companies", dedup_keys["find_careers_url"], conn
        )
        corpus["extract_jobs"] = self._load_by_keys("companies", dedup_keys["extract_jobs"], conn)
        corpus["description_reformat"] = self._load_by_keys(
            "jobs", dedup_keys["description_reformat"], conn
        )
        corpus["company_research"] = self._load_by_keys(
            "companies", dedup_keys["company_research"], conn
        )
        corpus["ai_nav_discovery"] = self._load_by_keys(
            "companies", dedup_keys["ai_nav_discovery"], conn
        )

        return corpus

    def _sample_parse_structured_fields(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample jobs with jd_full for parse_structured_fields."""
        rows = conn.execute(
            """
            SELECT dedup_key, jd_full
            FROM jobs
            WHERE jd_full IS NOT NULL AND LENGTH(jd_full) > 400
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = []
        for row in rows:
            result.append(dict(row))
            # Cache JD text to artifacts
            key = row["dedup_key"]
            cache_path = artifact_dir / "jd" / f"{_safe_cache_stem(key)}.txt"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(row["jd_full"], encoding="utf-8")

        return result

    def _sample_find_careers_url(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample companies with homepage_url for find_careers_url."""
        rows = conn.execute(
            """
            SELECT CAST(id AS TEXT) AS dedup_key, homepage_url
            FROM companies
            WHERE homepage_url IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = [dict(row) for row in rows]
        return result

    def _sample_extract_jobs(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample 50 companies for extract_jobs and cache HTML."""
        rows = conn.execute(
            """
            SELECT CAST(id AS TEXT) AS dedup_key, homepage_url
            FROM companies
            WHERE homepage_url IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = []
        for row in rows:
            result.append(dict(row))
            # Cache homepage HTML to artifacts/round_1/html/
            # Note: HTML fetching happens at Round 1 start per spec
            # This just records the companies to fetch

        return result

    def _sample_description_reformat(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample jobs with description for description_reformat."""
        rows = conn.execute(
            """
            SELECT dedup_key, description
            FROM jobs
            WHERE description IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = []
        for row in rows:
            result.append(dict(row))
            # Cache description to artifacts
            key = row["dedup_key"]
            cache_path = artifact_dir / "descriptions" / f"{_safe_cache_stem(key)}.txt"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(row["description"], encoding="utf-8")

        return result

    def _sample_company_research(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample companies for company_research."""
        rows = conn.execute(
            """
            SELECT CAST(id AS TEXT) AS dedup_key, name, homepage_url
            FROM companies
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = [dict(row) for row in rows]
        return result

    def _sample_ai_nav_discovery(
        self, n: int, conn: sqlite3.Connection, artifact_dir: Path
    ) -> list[dict]:
        """Sample companies with careers_nav_recipe for ai_nav_discovery."""
        rows = conn.execute(
            """
            SELECT CAST(id AS TEXT) AS dedup_key, careers_nav_recipe
            FROM companies
            WHERE careers_nav_recipe IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        result = []
        for row in rows:
            result.append(dict(row))
            # Cache recipe to artifacts/round_0/recipes/
            key = row["dedup_key"]
            recipe_path = artifact_dir / "recipes" / f"{_safe_cache_stem(key)}.json"
            recipe_path.parent.mkdir(parents=True, exist_ok=True)
            recipe_path.write_text(row["careers_nav_recipe"], encoding="utf-8")

        return result

    def _load_by_keys(
        self, table: str, dedup_keys: list[str], conn: sqlite3.Connection
    ) -> list[dict]:
        """Load rows from table by dedup_keys.

        For the `jobs` table the keying column is `dedup_key`. For the
        `companies` table production has no `dedup_key`; the sampler aliases
        `CAST(id AS TEXT) AS dedup_key`, so reload via the `id` column and
        expose `id` back as `dedup_key` for downstream consistency.
        """
        if not dedup_keys:
            return []

        placeholders = ",".join("?" * len(dedup_keys))
        if table == "companies":
            query = f"SELECT *, CAST(id AS TEXT) AS dedup_key FROM {table} WHERE CAST(id AS TEXT) IN ({placeholders})"
        else:
            query = f"SELECT * FROM {table} WHERE dedup_key IN ({placeholders})"
        rows = conn.execute(query, dedup_keys).fetchall()
        return [dict(row) for row in rows]
