"""Roll back an adopted override: delete file, hot-swap cache, audit, update health."""

from __future__ import annotations

import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import override_loader, surface_for_source
from job_finder.web.autoheal.audit import record_audit


def rollback_override(
    conn: sqlite3.Connection, source: str, reason: str, *, new_status: str = "degraded"
) -> bool:
    """Remove the override for *source* and audit ``rolled_back:<reason>``.

    new_status: 'degraded' for re-break rollbacks (the source is still broken);
    'healthy' for legacy-outperformed rollbacks (the legacy parser works again).
    Returns True when an effective override existed and was suppressed.
    Never touches ``heal_attempts`` (attempts are consumed at generate time and
    reset only at episode boundaries — see plan invariant I1).
    """
    surface = surface_for_source(source)
    file_key = source.split(":", 1)[1] if ":" in source else source
    removed = override_loader.delete_override(surface, file_key)
    override_loader.reload()
    conn.execute(  # I2: zero shadow state unconditionally, even when no file was removed
        "UPDATE source_health SET shadow_legacy_wins = 0, updated_at = ? WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()
    if not removed:
        return False
    record_audit(conn, source, surface, f"rolled_back:{reason}")
    conn.execute(
        "UPDATE source_health SET status = ?, updated_at = ? WHERE source = ?",
        (new_status, utc_now_iso(), source),
    )
    conn.commit()
    return True
