"""Repair pipeline_status FPs that the loose pre-2026-05-26 matcher created.

Background — see the 2026-05-26 audit. The pre-fix `_company_in_email`
matched generic words ("health", "company", "solutions") in the email body,
auto-promoting many unrelated jobs to phone_screen / applied. The new matcher
restricts attribution to subject + sender; the new auto-apply threshold also
requires either score>=4 or score>=3 with an ATS-domain sender.

This script re-evaluates every job whose current pipeline_status was set by
source='auto-detected', using the matcher+threshold currently imported from
job_finder.web.pipeline_detector. If a job's triggering detection would no
longer auto-apply under those rules, the script:

  1. Inserts a pipeline_event reverting the job to its prior status
     (source='auto-detect-repair'), or 'discovered' as the safe default.
  2. Updates the pipeline_status column.
  3. Marks the offending pipeline_detections row status='dismissed' so it
     never re-fires.

Defaults to dry-run. Pass --apply to actually write.
"""

import argparse
import io
import json
import sqlite3
import sys
from datetime import datetime

# Emoji-laden subject lines crash cp1252 on Windows — force UTF-8 stdout.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, ".")

from job_finder.web import user_data_dirs
from job_finder.web.pipeline_detector._signals import (
    _company_in_email,
    _sender_is_ats,
    _sender_matches_company,
)


def _would_still_auto_apply(job_company: str, det: sqlite3.Row) -> tuple[bool, str]:
    """Return (still_valid, reason) under the new matcher + threshold.

    Re-evaluates company attribution AND the two trust corroborators
    (``ats_domain``, ``sender_company``) from scratch — the stored
    ``matched_signals`` row predates the sender_company signal so its
    absence there cannot be trusted.
    """
    subject = det["email_subject"] or ""
    sender = det["email_from"] or ""

    company_now = _company_in_email(
        job_company,
        body="",
        subject=subject,
        from_address=sender,
    )
    if not company_now:
        return False, f"company '{job_company}' no longer matches subject/sender"

    try:
        signals = json.loads(det["matched_signals"] or "[]")
    except (json.JSONDecodeError, TypeError):
        signals = []
    # Score is "company + title + timing" portion that didn't change between
    # old and new rules; recompute corroborator signals against email metadata.
    title_or_timing = sum(1 for s in signals if s in ("title", "timing"))
    has_ats_now = _sender_is_ats(sender)
    has_sender_co_now = _sender_matches_company(sender, job_company)

    # New score = 1 (company) + title/timing portion + corroborators
    new_score = 1 + title_or_timing + (1 if has_ats_now else 0) + (1 if has_sender_co_now else 0)
    has_corroborator = has_ats_now or has_sender_co_now

    if new_score >= 4:
        return True, f"score={new_score} >= 4"
    if new_score >= 3 and has_corroborator:
        co = "ats_domain" if has_ats_now else "sender_company"
        return True, f"score={new_score} with {co}"
    return (
        False,
        f"score={new_score} ats={has_ats_now} sender_co={has_sender_co_now} fails new threshold",
    )


def _candidates(conn: sqlite3.Connection, sim_states: dict[str, str]) -> list[sqlite3.Row]:
    """Find jobs currently in phone_screen/applied whose state was set by auto-detect.

    ``sim_states`` shadows the DB so dry-run can advance jobs through several
    cascading FP reverts (e.g. Future was promoted to ``applied`` then to
    ``phone_screen`` from two unrelated emails; one pass unwinds the phone
    screen, the next unwinds the applied) without touching the database.
    """
    rows = conn.execute(
        """
        SELECT j.dedup_key, j.company, j.title, j.pipeline_status AS db_status,
               pe.timestamp AS auto_ts, pe.from_status AS prior_status,
               pe.to_status AS event_to
        FROM jobs j
        JOIN pipeline_events pe ON pe.job_id = j.dedup_key
        WHERE pe.source = 'auto-detected'
        ORDER BY pe.timestamp DESC
        """
    ).fetchall()
    out = []
    seen_for_pass: set[str] = set()
    for r in rows:
        jid = r["dedup_key"]
        if jid in seen_for_pass:
            continue
        current = sim_states.get(jid, r["db_status"])
        # Only the latest auto-detected event whose to_status matches the
        # current (possibly simulated) state is the active candidate.
        if r["event_to"] != current:
            continue
        if current not in ("phone_screen", "applied"):
            continue
        seen_for_pass.add(jid)
        out.append(r)
    return out


def _find_detection_for_transition(conn: sqlite3.Connection, job_id: str, event_ts: str):
    """Find the detection that triggered a specific auto-detected event.

    A job can have multiple auto-applied detections over time (Future's
    case). Match the one whose created_at is closest to (and not after)
    the event timestamp.
    """
    return conn.execute(
        "SELECT id, gmail_message_id, detection_type, confidence_score, "
        "       matched_signals, email_subject, email_from, created_at "
        "FROM pipeline_detections "
        "WHERE job_id = ? AND status = 'auto-applied' "
        "  AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (job_id, event_ts),
    ).fetchone()


def repair(db_path: str, apply: bool) -> dict:
    """Walk auto-detected promotions iteratively and revert FP cascades."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    summary = {"audited": 0, "kept": 0, "reverted": 0, "no_detection": 0, "details": []}
    sim_states: dict[str, str] = {}
    dismissed_detection_ids: set[int] = set()

    # Iterate until no candidate moves — each pass unwinds at most one
    # auto-detected step per job, so cascades take N passes for N steps.
    for _pass in range(10):
        cands = _candidates(conn, sim_states)
        # Skip ones already dismissed in this run
        cands = [
            c
            for c in cands
            if (sim_states.get(c["dedup_key"], c["db_status"]) or "discovered") != "discovered"
        ]
        if not cands:
            break

        any_change = False
        for row in cands:
            jid = row["dedup_key"]
            # Find the triggering detection (the auto-applied detection
            # closest in time to this specific event).
            det = _find_detection_for_transition(conn, jid, row["auto_ts"])
            # Skip detections we already dismissed this run
            while det is not None and det["id"] in dismissed_detection_ids:
                # Look further back
                det = conn.execute(
                    "SELECT id, gmail_message_id, detection_type, confidence_score, "
                    "       matched_signals, email_subject, email_from, created_at "
                    "FROM pipeline_detections "
                    "WHERE job_id = ? AND status = 'auto-applied' "
                    "  AND created_at < ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (jid, det["created_at"]),
                ).fetchone()

            if det is None:
                summary["no_detection"] += 1
                # Mark stable so we don't revisit
                sim_states[jid] = "__no_detection__"
                continue

            summary["audited"] += 1
            valid, reason = _would_still_auto_apply(row["company"], det)
            if valid:
                summary["kept"] += 1
                # Mark stable so this pass doesn't revisit (candidate query
                # would otherwise re-emit it next loop). Use current state as
                # a no-op key.
                sim_states[jid] = sim_states.get(jid, row["db_status"] or "discovered")
                continue

            prior: str = row["prior_status"] or "discovered"
            current: str = sim_states.get(jid, row["db_status"] or "discovered")
            sim_states[jid] = prior
            dismissed_detection_ids.add(det["id"])
            summary["reverted"] += 1
            any_change = True
            summary["details"].append(
                {
                    "job": jid,
                    "company": row["company"],
                    "current": current,
                    "revert_to": prior,
                    "trigger_subj": det["email_subject"],
                    "trigger_from": det["email_from"],
                    "reason": reason,
                    "event_ts": row["auto_ts"],
                }
            )

            if apply:
                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO pipeline_events "
                    "(job_id, from_status, to_status, timestamp, source, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        jid,
                        current,
                        prior,
                        now,
                        "auto-detect-repair",
                        f"FP revert: {reason}",
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET pipeline_status = ? WHERE dedup_key = ?",
                    (prior, jid),
                )
                conn.execute(
                    "UPDATE pipeline_detections SET status = 'dismissed' WHERE id = ?",
                    (det["id"],),
                )

        if not any_change:
            break

    if apply:
        conn.commit()
    conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the reverts (default is dry-run)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to jobs.db (defaults to user_data_dirs.db_path())",
    )
    args = parser.parse_args()

    db_path = args.db or str(user_data_dirs.db_path())
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    summary = repair(db_path, apply=args.apply)

    print(f"Audited:            {summary['audited']}")
    print(f"Kept (still valid): {summary['kept']}")
    print(f"Reverted:           {summary['reverted']}")
    print(f"No detection found: {summary['no_detection']}")
    print()
    if summary["details"]:
        print("--- Reverted jobs ---")
        for d in summary["details"]:
            print(f"  {d['company'][:25]:25}  {d['current']:14} -> {d['revert_to']:14}")
            print(f"    trigger: {d['trigger_from'][:40]!r:40}  {d['trigger_subj'][:60]!r}")
            print(f"    reason:  {d['reason']}")

    if not args.apply and summary["reverted"]:
        print("\n(dry-run: pass --apply to commit reverts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
