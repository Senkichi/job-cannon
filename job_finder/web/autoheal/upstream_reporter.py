"""Upstream contribution channel — consent-gated heal bundles (Phase D / D5).

Every adoption writes a scrubbed contribution bundle to
``<userdata>/heal_contrib/``. NOTHING leaves the machine from this module:
the dashboard renders pending bundles behind an explicit user action
(pre-filled GitHub issue link / copy button), and the optional maintainer
auto-PR path is a separate, default-off flag (see ``maintainer_pr``).

Bundle samples are already PII-scrubbed at corpus capture time
(``corpus_store.append_sample``); the bundle never re-reads raw inputs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from job_finder.json_utils import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Failing-sample clip: enough structure for a maintainer to reproduce the
# break without shipping a whole rendered page in every bundle.
MAX_SAMPLE_CHARS = 20_000


def _contrib_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    from job_finder.web.user_data_dirs import user_data_root

    return user_data_root() / "heal_contrib"


def build_bundle(conn: sqlite3.Connection, source: str, surface: str, recipe_dict: dict) -> dict:
    """Assemble a contribution bundle for an adopted recipe.

    ``failing_sample`` is the newest zero-yield ``corpus_sample.raw_text``
    (PII-scrubbed at capture), clipped to ``MAX_SAMPLE_CHARS``; ``drift`` is
    a ``source_health`` excerpt.
    """
    failing_sample = ""
    rows = conn.execute(
        "SELECT raw_text, output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC",
        (source,),
    ).fetchall()
    for raw_text, output_json in rows:
        try:
            job_count = int(json.loads(output_json).get("job_count", 0))
        except (ValueError, TypeError, AttributeError):
            job_count = 0
        if job_count == 0:
            failing_sample = (raw_text or "")[:MAX_SAMPLE_CHARS]
            break

    drift: dict = {}
    health = conn.execute(
        "SELECT consecutive_breaks, baseline_yield, last_signal "
        "FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if health is not None:
        drift = {
            "consecutive_breaks": health[0],
            "baseline_yield": health[1],
            "last_signal": health[2],
        }

    from job_finder.web.update_check import current_version

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "surface": surface,
        "recipe": recipe_dict,
        "failing_sample": failing_sample,
        "drift": drift,
        "created_at": utc_now_iso(),
        "app_version": current_version(),
    }


def write_bundle(bundle: dict, *, contrib_root: Path | str | None = None) -> Path:
    """Atomically write *bundle* to the contrib dir; return the final path.

    Filename: ``<sanitized source>-<UTC yyyymmddHHMMSS>.json`` (timestamp
    from the bundle's ``created_at`` — never ``datetime.now()``). A second
    adoption of the same source within the same second overwrites — newest
    bundle wins, which is the right dedup for an unreviewed queue.
    """
    out_dir = _contrib_root(contrib_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_source = re.sub(r"[^A-Za-z0-9._-]+", "-", str(bundle.get("source") or "unknown"))
    stamp = re.sub(r"[^0-9]", "", str(bundle.get("created_at") or utc_now_iso()))[:14]
    out_path = out_dir / f"{safe_source}-{stamp}.json"

    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return out_path


def pending_bundles(*, contrib_root: Path | str | None = None) -> list[dict]:
    """All bundles on disk, newest first, each with its ``filename``. Never raises."""
    try:
        root = _contrib_root(contrib_root)
        if not root.is_dir():
            return []
        bundles: list[dict] = []
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            data["filename"] = path.name
            bundles.append(data)
        bundles.sort(key=lambda b: str(b.get("created_at") or ""), reverse=True)
        return bundles
    except Exception:
        logger.exception("upstream_reporter: pending_bundles failed")
        return []
