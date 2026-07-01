"""Application package persistence — prepared application review queue."""

import json
import sqlite3

from job_finder.json_utils import safe_json_load, utc_now_iso


def upsert_application(
    conn: sqlite3.Connection,
    job_id: str,
    resume_content: str,
    form_mapping: dict,
    drafted_answers: dict,
) -> int:
    """Insert (or replace) a pending application package for a job. Returns the row id.

    Serializes form_mapping / drafted_answers to JSON. Sets status='pending',
    created_at=utc_now_iso(), resolved_at=NULL. On conflict with the UNIQUE
    job_id, overwrites the existing package and re-opens it as 'pending'.
    """
    now = utc_now_iso()
    form_mapping_json = json.dumps(form_mapping)
    drafted_answers_json = json.dumps(drafted_answers)

    cursor = conn.execute(
        """INSERT INTO applications (job_id, status, created_at, resolved_at, resume_content, form_mapping_json, drafted_answers_json)
           VALUES (?, 'pending', ?, NULL, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             status='pending',
             created_at=?,
             resolved_at=NULL,
             resume_content=excluded.resume_content,
             form_mapping_json=excluded.form_mapping_json,
             drafted_answers_json=excluded.drafted_answers_json""",
        (
            job_id,
            now,
            resume_content,
            form_mapping_json,
            drafted_answers_json,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_application(conn: sqlite3.Connection, application_id: int) -> dict | None:
    """Return one application row as a dict (json columns parsed to dict), or None."""
    row = conn.execute(
        "SELECT * FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()

    if row is None:
        return None

    app = dict(row)
    app["form_mapping"] = safe_json_load(app.pop("form_mapping_json"), {})
    app["drafted_answers"] = safe_json_load(app.pop("drafted_answers_json"), {})
    return app


def get_application_by_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Return the application row for a job (dict, json parsed), or None."""
    row = conn.execute(
        "SELECT * FROM applications WHERE job_id = ?",
        (job_id,),
    ).fetchone()

    if row is None:
        return None

    app = dict(row)
    app["form_mapping"] = safe_json_load(app.pop("form_mapping_json"), {})
    app["drafted_answers"] = safe_json_load(app.pop("drafted_answers_json"), {})
    return app


def resolve_application(conn: sqlite3.Connection, application_id: int, resolution: str) -> None:
    """Set status to 'approved' or 'rejected' and resolved_at=utc_now_iso().
    Raise ValueError for any other resolution.
    """
    _VALID_RESOLUTIONS = ("approved", "rejected")
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(
            f"Invalid resolution: {resolution!r}. Must be one of: {', '.join(_VALID_RESOLUTIONS)}."
        )
    now = utc_now_iso()
    conn.execute(
        "UPDATE applications SET status = ?, resolved_at = ? WHERE id = ?",
        (resolution, now, application_id),
    )
    conn.commit()
