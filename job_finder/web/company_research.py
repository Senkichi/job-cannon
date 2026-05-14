"""On-demand company research service.

Follows the resume-generation async lifecycle pattern:
  pending → generating → done | error

Exports:
    get_cached_company_research: Check for a recent cached research result.
    start_company_research: Insert a pending row and launch background work.
    run_company_research_background: Background thread entry point.
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime

import anthropic

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)

# Cache TTL in hours — research older than this is considered stale
_CACHE_TTL_HOURS = 72


def get_cached_company_research(
    conn: sqlite3.Connection,
    company_id: int,
    ttl_hours: int = _CACHE_TTL_HOURS,
) -> dict | None:
    """Return the most recent done/generating research row within TTL.

    Args:
        conn: Open sqlite3 connection.
        company_id: Company row ID.
        ttl_hours: Maximum age of cached research in hours.

    Returns:
        Research row dict if a valid cache hit exists, None otherwise.
    """
    row = conn.execute(
        """SELECT * FROM company_research
           WHERE company_id = ?
             AND status IN ('done', 'generating', 'pending')
           ORDER BY id DESC LIMIT 1""",
        (company_id,),
    ).fetchone()

    if row is None:
        return None

    row_dict = dict(row)

    # Done rows: check TTL
    if row_dict["status"] == "done" and row_dict.get("completed_at"):
        try:
            completed = datetime.fromisoformat(row_dict["completed_at"])
            age_hours = (datetime.now(UTC) - completed.replace(tzinfo=UTC)).total_seconds() / 3600
            if age_hours > ttl_hours:
                return None  # Stale cache
        except (ValueError, TypeError):
            pass

    return row_dict


def start_company_research(
    conn: sqlite3.Connection,
    company_id: int,
    db_path: str,
    config: dict,
) -> int:
    """Insert a pending research row and launch background generation.

    Args:
        conn: Open sqlite3 connection.
        company_id: Company row ID.
        db_path: Path to SQLite database (for background thread).
        config: Application config dict.

    Returns:
        The new research row ID.
    """
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO company_research (company_id, status, requested_at) VALUES (?, ?, ?)",
        (company_id, "generating", now),
    )
    research_id = cursor.lastrowid
    conn.commit()

    thread = threading.Thread(
        target=run_company_research_background,
        args=(research_id, company_id, db_path, config),
        daemon=True,
    )
    thread.start()

    return research_id


def run_company_research_background(
    research_id: int,
    company_id: int,
    db_path: str,
    config: dict,
) -> None:
    """Generate company research in a background thread.

    Opens its own sqlite3 connection (thread-safe). Loads company name,
    calls Haiku for synthesis, and updates the research row.

    Args:
        research_id: The company_research row ID.
        company_id: The company row ID.
        db_path: Path to SQLite database file.
        config: Application config dict.
    """
    with standalone_connection(db_path) as conn:
        try:
            company = conn.execute(
                "SELECT name, homepage_url, industry, company_size FROM companies WHERE id = ?",
                (company_id,),
            ).fetchone()

            if company is None:
                raise ValueError(f"Company not found: {company_id}")

            name = company["name"]
            homepage = company["homepage_url"] or ""
            industry = company["industry"] or ""
            size = company["company_size"] or ""

            # Build prompt
            system_prompt = (
                "You are a company research analyst. Produce a concise research brief "
                "about the given company covering: mission/products, recent news, culture, "
                "interview tips, and key competitors. Be specific and factual."
            )
            user_msg = (
                f"Research the company: {name}\n"
                f"Homepage: {homepage}\n"
                f"Industry: {industry}\n"
                f"Size: {size}\n\n"
                "Provide a structured research brief."
            )

            client = anthropic.Anthropic()
            result_obj = call_model(
                tier="low",
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                conn=conn,
                config=config,
                job_id=f"company_{company_id}",
                purpose="company_research",
                max_tokens=2048,
                client=client,
            )

            research_text = (
                result_obj.data
                if isinstance(result_obj.data, str)
                else json.dumps(result_obj.data)
            )
            cost_usd = result_obj.cost_usd
            now = datetime.now(UTC).isoformat()

            conn.execute(
                """UPDATE company_research
                   SET status = 'done',
                       research_json = ?,
                       cost_usd = ?,
                       completed_at = ?
                   WHERE id = ?""",
                (research_text, cost_usd, now, research_id),
            )
            conn.commit()
            logger.info(
                "Company research complete for company %d (cost=%.4f)", company_id, cost_usd
            )

        except Exception as e:
            error_msg = str(e)[:500]
            logger.exception("Company research failed for company %d: %s", company_id, e)
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE company_research SET status = 'error', error_msg = ?, completed_at = ? WHERE id = ?",
                (error_msg, now, research_id),
            )
            conn.commit()
