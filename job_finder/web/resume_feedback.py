"""Resume feedback loop — Drive polling, diff extraction, Sonnet preference extraction.

Polls Google Drive every 30 minutes for edits to generated resume documents.
When a resume has been modified, extracts text diff and uses Sonnet to identify
phrasing, content, and structural preferences. Stores them in
resume_preferences_detected for display on /feedback page.

Exports:
    run_drive_feedback_poll(db_path, config) -> dict
    run_preference_consolidation(db_path, config) -> dict
    poll_resume_for_changes(service, file_id, last_polled_at) -> str | None

Pattern: own sqlite3.connect(db_path) connection (thread-safe for APScheduler).
Follows stale_detector.py and resume_generator.py background patterns.
"""

import difflib
import io
import logging
import re
import sqlite3
from datetime import datetime, timezone

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from job_finder.web.claude_client import BudgetExceededError
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import call_model
from job_finder.web.drive_uploader import get_drive_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema for Sonnet preference extraction
# ---------------------------------------------------------------------------

PREFERENCE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "phrasing_preferences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "preference": {"type": "string"},
                    "example_before": {"type": "string"},
                    "example_after": {"type": "string"},
                },
                "required": ["preference", "example_before", "example_after"],
                "additionalProperties": False,
            },
        },
        "content_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change_type": {"type": "string", "enum": ["addition", "removal"]},
                    "description": {"type": "string"},
                },
                "required": ["change_type", "description"],
                "additionalProperties": False,
            },
        },
        "structural_preferences": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["phrasing_preferences", "content_changes", "structural_preferences"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Module-level cache for original exported text (cleared on restart)
# Keyed by resume_generation id. Acceptable since polling is periodic.
#
# Thread-safety: both run_drive_feedback_poll and run_preference_consolidation
# are scheduled with max_instances=1 (see scheduler.py), so only one APScheduler
# thread accesses this dict at a time. The check-then-act at lines 369-372 and
# 403 is not atomic, but concurrent execution is prevented by max_instances=1.
# ---------------------------------------------------------------------------
_original_text_cache: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Drive polling
# ---------------------------------------------------------------------------

def _extract_file_id_from_url(doc_url: str) -> str | None:
    """Extract Google Drive/Docs file ID from a URL.

    Handles:
    - https://docs.google.com/document/d/FILE_ID/edit
    - https://drive.google.com/file/d/FILE_ID/view

    Returns file ID string or None if not parseable.
    """
    if not doc_url:
        return None
    # Match /d/FILE_ID pattern (Google Docs and Drive share this format)
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", doc_url)
    if match:
        return match.group(1)
    return None


def poll_resume_for_changes(
    service, file_id: str, last_polled_at: str | None
) -> tuple[str | None, str]:
    """Return (text, modifiedTime) — text is None if unchanged or non-Google-Doc.

    Args:
        service: Authenticated Drive v3 service instance.
        file_id: Google Drive file ID.
        last_polled_at: RFC 3339 timestamp of last poll, or None for first poll.

    Returns:
        Tuple of (text, modifiedTime). text is None if no change or skipped.
        modifiedTime is always returned for updating last_drive_polled_at.
    """
    try:
        meta = service.files().get(
            fileId=file_id, fields="id,modifiedTime,mimeType"
        ).execute()
    except HttpError as e:
        logger.error("Drive API error fetching metadata for file %s: %s", file_id, e)
        raise
    except Exception as e:
        logger.error("Unexpected error fetching Drive metadata for file %s: %s", file_id, e)
        raise

    modified_time = meta.get("modifiedTime", "")

    # Skip if not modified since last poll
    if last_polled_at and modified_time <= last_polled_at:
        return None, modified_time

    mime_type = meta.get("mimeType", "")
    if mime_type != "application/vnd.google-apps.document":
        # Skip .docx and other binary files (export_media only works on Google Docs)
        logger.debug("Skipping non-Google-Doc file %s (mimeType=%s)", file_id, mime_type)
        return None, modified_time

    # Google Doc — export as plain text
    try:
        request = service.files().export_media(fileId=file_id, mimeType="text/plain")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except HttpError as e:
        logger.error("Drive API error exporting file %s: %s", file_id, e)
        raise
    except Exception as e:
        logger.error("Unexpected error exporting Drive file %s: %s", file_id, e)
        raise

    return buf.getvalue().decode("utf-8"), modified_time


# ---------------------------------------------------------------------------
# Preference extraction
# ---------------------------------------------------------------------------

def _extract_preferences(
    diff_text: str,
    conn: sqlite3.Connection,
    job_id: str,
    config: dict,
) -> list[dict]:
    """Call Sonnet to extract resume editing preferences from a diff.

    Args:
        diff_text: Unified diff string showing what changed.
        conn: Open SQLite connection for cost gating and recording.
        job_id: Job dedup_key for cost attribution.
        config: Application config dict.

    Returns:
        List of preference dicts with keys: preference_type, preference_text,
        example_before, example_after.
    """
    system_prompt = (
        "You are analyzing edits made to a resume generated by AI. "
        "The user has modified the resume in Google Docs. "
        "Analyze the unified diff below and extract what this person prefers "
        "in their resumes: phrasing choices, content additions/removals, "
        "and structural preferences.\n\n"
        "Focus on meaningful edits, not whitespace or formatting artifacts."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Here is the unified diff of changes made to the resume:\n\n"
                f"```diff\n{diff_text}\n```\n\n"
                "Extract resume preferences from these edits."
            ),
        }
    ]

    try:
        client = None
        if anthropic is not None:
            try:
                client = anthropic.Anthropic()
            except Exception:
                pass
        result_obj = call_model(
            tier="sonnet",
            system=system_prompt,
            messages=messages,
            conn=conn,
            config=config,
            output_schema=PREFERENCE_EXTRACTION_SCHEMA,
            job_id=job_id,
            purpose="sonnet_resume_feedback",
            max_tokens=1024,
            client=client,
        )
        result = result_obj.data
    except BudgetExceededError:
        logger.info("Budget exceeded — skipping preference extraction")
        return []
    except Exception as e:
        logger.error("Preference extraction failed: %s", e)
        return []

    # Normalize result into flat list of preference dicts
    preferences = []

    for pref in result.get("phrasing_preferences", []):
        preferences.append({
            "preference_type": "phrasing",
            "preference_text": pref.get("preference", ""),
            "example_before": pref.get("example_before"),
            "example_after": pref.get("example_after"),
        })

    for change in result.get("content_changes", []):
        change_type = change.get("change_type", "addition")
        pref_type = "content_addition" if change_type == "addition" else "content_removal"
        preferences.append({
            "preference_type": pref_type,
            "preference_text": change.get("description", ""),
            "example_before": None,
            "example_after": None,
        })

    for struct in result.get("structural_preferences", []):
        preferences.append({
            "preference_type": "structural",
            "preference_text": struct,
            "example_before": None,
            "example_after": None,
        })

    return preferences


def _store_preferences(
    conn: sqlite3.Connection,
    job_id: str,
    preferences: list[dict],
) -> int:
    """Insert preferences into resume_preferences_detected.

    Args:
        conn: Open SQLite connection.
        job_id: Job dedup_key to associate preferences with.
        preferences: List of preference dicts from _extract_preferences().

    Returns:
        Count of preferences inserted.
    """
    if not preferences:
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for pref in preferences:
        conn.execute(
            """INSERT INTO resume_preferences_detected
               (job_id, preference_type, preference_text, example_before, example_after,
                accepted, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                pref.get("preference_type", "phrasing"),
                pref.get("preference_text", ""),
                pref.get("example_before"),
                pref.get("example_after"),
                1,  # auto-accepted by default
                now,
            ),
        )
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Main poll runner
# ---------------------------------------------------------------------------

def run_drive_feedback_poll(db_path: str, config: dict) -> dict:
    """Poll Google Drive for resume edits and extract preferences.

    Opens its own sqlite3 connection (thread-safe for APScheduler background jobs).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict.

    Returns:
        Summary dict: {resumes_polled, changes_detected, preferences_extracted}.
    """
    resumes_polled = 0
    changes_detected = 0
    preferences_extracted = 0

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, job_id, doc_url, last_drive_polled_at "
            "FROM resume_generations "
            "WHERE doc_url IS NOT NULL AND status='done'"
        ).fetchall()

        if not rows:
            return {
                "resumes_polled": 0,
                "changes_detected": 0,
                "preferences_extracted": 0,
            }

        try:
            service = get_drive_service()
        except Exception as e:
            logger.error("Drive service unavailable for feedback poll: %s", e)
            return {
                "resumes_polled": 0,
                "changes_detected": 0,
                "preferences_extracted": 0,
                "error": str(e),
            }

        for row in rows:
            gen_id = row["id"]
            job_id = row["job_id"]
            doc_url = row["doc_url"]
            last_polled_at = row["last_drive_polled_at"]

            file_id = _extract_file_id_from_url(doc_url)
            if not file_id:
                logger.warning("Cannot extract file_id from doc_url: %s", doc_url)
                continue

            try:
                current_text, new_polled_at = poll_resume_for_changes(
                    service, file_id, last_polled_at
                )
                resumes_polled += 1

                if current_text is not None:
                    changes_detected += 1

                    # Compute diff against original (cache-based)
                    original_text = _original_text_cache.get(gen_id, "")
                    if not original_text:
                        # First poll — store current text as "original baseline"
                        _original_text_cache[gen_id] = current_text
                        # Update timestamp and continue (no diff yet)
                        conn.execute(
                            "UPDATE resume_generations SET last_drive_polled_at=? WHERE id=?",
                            (new_polled_at, gen_id),
                        )
                        conn.commit()
                        continue

                    # Generate unified diff
                    diff_lines = list(difflib.unified_diff(
                        original_text.splitlines(keepends=True),
                        current_text.splitlines(keepends=True),
                        fromfile="original",
                        tofile="edited",
                    ))

                    # Only process non-trivial diffs (more than whitespace changes)
                    meaningful_diff = [
                        line for line in diff_lines
                        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                        and line.strip() not in ("+", "-", "")
                    ]

                    if meaningful_diff:
                        diff_text = "".join(diff_lines)
                        prefs = _extract_preferences(diff_text, conn, job_id, config)
                        stored = _store_preferences(conn, job_id, prefs)
                        preferences_extracted += stored

                        # Update the cached "original" to current for next diff
                        _original_text_cache[gen_id] = current_text

                # Update last_drive_polled_at regardless of whether text changed
                if new_polled_at:
                    conn.execute(
                        "UPDATE resume_generations SET last_drive_polled_at=? WHERE id=?",
                        (new_polled_at, gen_id),
                    )
                    conn.commit()

            except Exception as e:
                logger.error(
                    "Drive poll error for generation %s (job=%s): %s", gen_id, job_id, e
                )

        result = {
            "resumes_polled": resumes_polled,
            "changes_detected": changes_detected,
            "preferences_extracted": preferences_extracted,
        }
        logger.info("Drive feedback poll: %s", result)
        return result


# ---------------------------------------------------------------------------
# Preference consolidation
# ---------------------------------------------------------------------------

def run_preference_consolidation(db_path: str, config: dict) -> dict:
    """Consolidate similar accepted preferences into canonical rules.

    Triggered when preference count > 10 or weekly via APScheduler.
    Opens its own sqlite3 connection (thread-safe for APScheduler).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict.

    Returns:
        Summary dict: {consolidated, count} or {consolidated, original_count, consolidated_count}.
    """
    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM resume_preferences_detected "
            "WHERE accepted=1 AND applied_at IS NULL "
            "ORDER BY preference_type, detected_at"
        ).fetchall()
        count = len(rows)

        if count <= 10:
            return {"consolidated": False, "count": count}

        # Group preferences for Sonnet consolidation
        all_prefs_text = []
        for row in rows:
            all_prefs_text.append(
                f"[{row['preference_type']}] {row['preference_text']}"
                + (f"\n  Before: {row['example_before']}" if row["example_before"] else "")
                + (f"\n  After: {row['example_after']}" if row["example_after"] else "")
            )

        system_prompt = (
            "You are consolidating a list of resume editing preferences into canonical rules. "
            "Merge similar preferences, remove redundancies, and produce a concise set of "
            "canonical writing guidelines organized by type."
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Here are {count} accumulated resume editing preferences:\n\n"
                    + "\n\n".join(all_prefs_text)
                    + "\n\nConsolidate these into a canonical set of preferences. "
                    "Merge similar ones, keep distinct ones separate."
                ),
            }
        ]

        try:
            client = None
            if anthropic is not None:
                try:
                    client = anthropic.Anthropic()
                except Exception:
                    pass
            result_obj = call_model(
                tier="sonnet",
                system=system_prompt,
                messages=messages,
                conn=conn,
                config=config,
                output_schema=PREFERENCE_EXTRACTION_SCHEMA,
                job_id=None,
                purpose="sonnet_preference_consolidation",
                max_tokens=1024,
                client=client,
            )
            result = result_obj.data
        except BudgetExceededError:
            logger.info("Budget exceeded during consolidation")
            return {"consolidated": False, "count": count, "budget_exceeded": True}
        except Exception as e:
            logger.error("Preference consolidation failed: %s", e)
            return {"consolidated": False, "count": count, "error": str(e)}

        # Mark old preferences as superseded
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_ids = [row["id"] for row in rows]
        for old_id in old_ids:
            conn.execute(
                "UPDATE resume_preferences_detected SET applied_at=? WHERE id=?",
                (now, old_id),
            )

        # Get the first job_id from the preferences for new rows
        first_job_id = rows[0]["job_id"] if rows else None

        # Insert new consolidated preferences
        new_preferences = []
        for pref in result.get("phrasing_preferences", []):
            new_preferences.append({
                "preference_type": "phrasing",
                "preference_text": pref.get("preference", ""),
                "example_before": pref.get("example_before"),
                "example_after": pref.get("example_after"),
            })
        for change in result.get("content_changes", []):
            change_type = change.get("change_type", "addition")
            pref_type = "content_addition" if change_type == "addition" else "content_removal"
            new_preferences.append({
                "preference_type": pref_type,
                "preference_text": change.get("description", ""),
                "example_before": None,
                "example_after": None,
            })
        for struct in result.get("structural_preferences", []):
            new_preferences.append({
                "preference_type": "structural",
                "preference_text": struct,
                "example_before": None,
                "example_after": None,
            })

        if first_job_id:
            new_count = _store_preferences(conn, first_job_id, new_preferences)
        else:
            new_count = 0

        conn.commit()

        result_summary = {
            "consolidated": True,
            "original_count": count,
            "consolidated_count": new_count,
        }
        logger.info("Preference consolidation: %s", result_summary)
        return result_summary
