"""Haiku-assisted job description reformatting.

Reformats raw job descriptions (pipe-separated, bullet lists, messy email-parsed
formatting) into clean section/paragraph style resembling a real job posting.

Per user decision: ALL job descriptions are reformatted — not just merged ones.
The description_reformatted flag (added by Migration 6 in Plan 01) prevents
re-running on already-processed jobs.

Design:
  - reformat_description: Single-job reformatting via Haiku API call.
  - run_description_reformat_pass: One-time background pass over all unformatted jobs.
  - Both are graceful-degradation: failures return original text unchanged.
  - Already-well-formatted descriptions (2+ section headers) are skipped.

Cost note: Haiku is ~$0.0003/call. With ~200 existing jobs, total is ~$0.06.

Exports:
    reformat_description: Reformat a single description string via Haiku.
    run_description_reformat_pass: One-time background pass over all unformatted jobs.
"""

import logging
import re
from typing import Optional, Any

from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Regex pattern for common section headers (2+ indicates already formatted)
_SECTION_HEADER_PATTERN = re.compile(
    r"(?:About|Overview|Summary|Responsibilities|Requirements|Qualifications|Benefits|What You|Minimum|Preferred|Nice to Have|The Role|Your Role|Who You Are|What We)",
    re.IGNORECASE,
)

# Minimum number of section headers to consider a description already formatted
_ALREADY_FORMATTED_THRESHOLD = 2

# System prompt for Haiku reformatting
_SYSTEM_PROMPT = (
    "You are a job description formatter. Reformat the following job description into "
    "clean, professional sections with headers and paragraphs — like a real job posting. "
    "Use section headers like 'About the Role', 'Responsibilities', 'Requirements', "
    "'Qualifications', 'Benefits', etc. as appropriate. Convert bullet lists and "
    "pipe-separated items into proper paragraphs or clean bullet lists. Preserve all "
    "factual content — do not add or remove information. Return ONLY the reformatted text."
)

def reformat_description(
    description: Optional[str],
    conn: Any = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """Use Haiku to reformat a job description into section/paragraph style.

    Takes raw description text (pipe-separated, bullet lists, or messy formatting)
    and returns clean section/paragraph text resembling a real job posting.

    Per user decision: "All job descriptions reformatted to section/paragraph style
    (like real job postings) — applies to ALL jobs, not just merged ones."

    Returns original description on any failure (graceful degradation).

    Args:
        description: Raw job description text to reformat. Returns as-is if None/empty.
        client: Anthropic client instance (injected for testability).
        conn: Optional SQLite connection for cost recording.
        config: Optional application config dict.

    Returns:
        Reformatted description text, or original if skipped/failed.
    """
    if not description:
        return description

    if config is None:
        config = {}

    # Skip if already well-formatted: check for 2+ section headers
    header_count = len(_SECTION_HEADER_PATTERN.findall(description))
    if header_count >= _ALREADY_FORMATTED_THRESHOLD:
        return description

    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("haiku", DEFAULT_MODEL_HAIKU)
    )

    try:
        result, _cost = call_claude(
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description[:4000]}],
            output_schema=None,
            conn=conn,
            job_id=None,
            purpose="description_reformat",
            config=config,
            max_tokens=2048,
        )

        # call_claude returns dict — extract the text field
        if isinstance(result, dict):
            reformatted = result.get("text", "")
        else:
            reformatted = str(result)

        if reformatted and reformatted.strip():
            return reformatted.strip()

        return description

    except Exception as e:
        logger.warning("reformat_description failed (returning original): %s", e)
        return description

def run_description_reformat_pass(
    db_path: str,
    config: Optional[dict] = None,
) -> int:
    """One-time background pass to reformat all job descriptions.

    Processes jobs where description_reformatted=0 and description IS NOT NULL.
    Sets description_reformatted=1 after each job is successfully reformatted.
    Opens own sqlite3 connection (thread-safe for background execution).

    Called from db_migrate.py post-migration hook or as a manual trigger.
    Returns count of jobs reformatted.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Optional application config dict.

    Returns:
        Count of jobs where reformatting was attempted (including already-formatted).
    """
    if config is None:
        config = {}

    # Guard: skip in test mode if db_path is :memory: (edge case)
    if db_path == ":memory:":
        return 0

    try:
        with standalone_connection(db_path) as conn:

            rows = conn.execute(
                "SELECT dedup_key, description FROM jobs "
                "WHERE description_reformatted = 0 AND description IS NOT NULL"
            ).fetchall()

            reformatted_count = 0

            for row in rows:
                dedup_key = row["dedup_key"]
                original = row["description"]

                try:
                    reformatted = reformat_description(
                        original, conn=conn, config=config
                    )

                    if reformatted != original and reformatted is not None:
                        # Text changed — update both description and flag
                        conn.execute(
                            "UPDATE jobs SET description = ?, description_reformatted = 1 "
                            "WHERE dedup_key = ?",
                            (reformatted, dedup_key),
                        )
                        reformatted_count += 1
                    else:
                        # Text unchanged (already formatted or Haiku returned same text)
                        # Mark as processed so it's not retried
                        conn.execute(
                            "UPDATE jobs SET description_reformatted = 1 WHERE dedup_key = ?",
                            (dedup_key,),
                        )

                    conn.commit()

                except Exception as e:
                    logger.warning(
                        "Failed to reformat description for '%s' (non-fatal): %s",
                        dedup_key,
                        e,
                    )
                    # Mark as processed anyway to avoid infinite retry loop
                    try:
                        conn.execute(
                            "UPDATE jobs SET description_reformatted = 1 WHERE dedup_key = ?",
                            (dedup_key,),
                        )
                        conn.commit()
                    except Exception:
                        logger.debug("description reformat commit failed", exc_info=True)

            logger.info("Reformatted %d job descriptions", reformatted_count)
            return reformatted_count

    except Exception as e:
        logger.warning("run_description_reformat_pass failed: %s", e)
        return 0
