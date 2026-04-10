"""Compute legitimacy signals for ghost job detection.

Provides compute_legitimacy_signals() which computes lightweight legitimacy
indicators from DB state. These signals are injected into the Haiku scoring
prompt so ghost/phantom job postings (reposted perpetually with no intent to
hire) are penalized before Sonnet spends tokens on deep evaluation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

# Generic filler phrases that signal low-effort / perpetual postings.
_FILLER_PHRASES = frozenset([
    "fast-paced environment",
    "team player",
    "self-starter",
    "wear many hats",
    "other duties as assigned",
    "competitive salary",
    "great benefits",
    "dynamic team",
    "exciting opportunity",
    "rock star",
    "ninja",
    "guru",
])

_WORD_RE = re.compile(r"\b\w+\b")


def compute_legitimacy_signals(job_row: dict, conn) -> dict:
    """Compute legitimacy signals from DB state.

    Args:
        job_row: Row dict from jobs table (must have id, dedup_key,
                 first_seen_at, description/jd_full, salary_min).
        conn: SQLite connection (may be None for test isolation).

    Returns:
        Dict with keys: posting_age_days, source_count, has_salary,
        description_length, filler_ratio, legitimacy_note.
    """
    signals = {}

    # 1. Posting age
    first_seen = job_row.get("first_seen_at") or job_row.get("first_seen") or job_row.get("created_at")
    if first_seen:
        if isinstance(first_seen, str):
            first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - first_seen).days
        signals["posting_age_days"] = age
    else:
        signals["posting_age_days"] = None

    # 2. Source diversity (proxy for repost frequency)
    dedup_key = job_row.get("dedup_key", "")
    if dedup_key and conn:
        try:
            row = conn.execute(
                "SELECT sources FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if row and row["sources"]:
                try:
                    sources = json.loads(row["sources"])
                    signals["source_count"] = len(sources) if isinstance(sources, list) else 1
                except (json.JSONDecodeError, TypeError):
                    signals["source_count"] = 1
            else:
                signals["source_count"] = 1
        except Exception:
            signals["source_count"] = 1
    else:
        signals["source_count"] = 1

    # 3. Salary transparency
    signals["has_salary"] = job_row.get("salary_min") is not None

    # 4. JD specificity
    text = job_row.get("jd_full") or job_row.get("description") or ""
    signals["description_length"] = len(text)

    if text and len(text) > 100:
        words = _WORD_RE.findall(text.lower())
        total_words = len(words)
        filler_count = sum(
            1 for phrase in _FILLER_PHRASES
            if phrase in text.lower()
        )
        signals["filler_ratio"] = round(filler_count / max(total_words / 50, 1), 2)
    else:
        signals["filler_ratio"] = 0.0

    # 5. Build human-readable note for the Haiku prompt
    notes = []
    age = signals["posting_age_days"]
    if age is not None and age > 60:
        notes.append(f"WARNING: Posting is {age} days old")
    elif age is not None and age > 30:
        notes.append(f"Note: Posting is {age} days old")

    if signals["source_count"] >= 4:
        notes.append(
            f"Appears across {signals['source_count']} sources (possible perpetual repost)"
        )

    if signals["description_length"] < 200 and signals["description_length"] > 0:
        notes.append("Very short job description (possible placeholder)")

    if signals["filler_ratio"] > 3.0:
        notes.append("High ratio of generic filler language")

    if not signals["has_salary"]:
        notes.append("No salary information posted")

    signals["legitimacy_note"] = "; ".join(notes) if notes else ""

    return signals
