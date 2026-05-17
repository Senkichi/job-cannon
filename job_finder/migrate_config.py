"""Migrate config.yaml from the pre-Phase-40 nested providers.scoring schema
to the Phase 40 flat ``providers.{primary, fallback_chain, overrides}`` shape.

Triggered manually by users whose boot fails with the
``Old config schema detected`` ConfigError from
:func:`job_finder.config.validate_required_sections`.

Usage::

    uv run python -m job_finder.migrate_config              # writes user_data config.yaml
    uv run python -m job_finder.migrate_config /tmp/foo.yaml  # operate on an explicit path (used by tests)

Behavior:

- If ``providers.primary`` is already present → exits 0 without writing.
- If ``providers.scoring.provider`` is present → migrates in place, after
  writing a timestamped ``.bak.<ts>`` next to the file.
- Otherwise → exits 1, points the user at ``config.example.yaml``.

Per-entry ``model:`` values on cascade entries (including the primary) are
preserved as ``providers.overrides.<provider>.score`` so users who pinned
non-default models do not silently lose them.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from os import PathLike, replace
from pathlib import Path
from typing import Final

import yaml

MIGRATED: Final[str] = "migrated"
ALREADY_MIGRATED: Final[str] = "already-migrated"
UNKNOWN_SHAPE: Final[str] = "unknown-shape"


def _translate(old_providers: dict) -> dict:
    """Translate the old nested shape into the Phase 40 flat shape.

    ``old_providers`` is the ``providers`` subtree from the old config.
    Returns the replacement ``providers`` subtree.
    """
    scoring = old_providers["scoring"]
    primary_name: str = scoring["provider"]
    primary_model: str | None = scoring.get("model")
    fallback_entries: list[dict] = list(scoring.get("fallback_chain", []))

    # Provider names for the flat fallback_chain (strings only).
    fallback_names: list[str] = [entry["provider"] for entry in fallback_entries]

    # Build overrides only for entries that actually pin a model. Workload
    # key is "score" — the old providers.scoring tier maps to the v3.0
    # "score" workload.
    overrides: dict[str, dict[str, str]] = {}
    if primary_model:
        overrides.setdefault(primary_name, {})["score"] = primary_model
    for entry in fallback_entries:
        model = entry.get("model")
        if model:
            overrides.setdefault(entry["provider"], {})["score"] = model

    new_providers: dict = {
        "primary": primary_name,
        "fallback_chain": fallback_names,
        "overrides": overrides,
    }

    # Preserve sibling keys verbatim (daily_limits, throttle_delays, etc).
    for k, v in old_providers.items():
        if k == "scoring":
            continue
        new_providers.setdefault(k, v)

    return new_providers


def _atomic_write(path: Path, data: dict) -> None:
    """Write YAML atomically via tmpfile + os.replace in the same directory."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def migrate_file(path: str | PathLike[str]) -> str:
    """Migrate the config at ``path`` in place.

    Returns one of MIGRATED, ALREADY_MIGRATED, UNKNOWN_SHAPE. Prints a
    user-visible status line in each case. Side effects: creates a
    ``<path>.bak.<timestamp>`` backup and overwrites ``path`` only when
    returning MIGRATED.
    """
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    providers = cfg.get("providers", {}) or {}

    if "primary" in providers:
        print(f"Already in Phase 40 schema: {p}")
        return ALREADY_MIGRATED

    if not isinstance(providers.get("scoring"), dict) or "provider" not in providers["scoring"]:
        print(
            f"Cannot auto-migrate {p}: no providers.scoring.provider key found. "
            f"See config.example.yaml for the expected structure."
        )
        return UNKNOWN_SHAPE

    new_providers = _translate(providers)
    new_cfg = dict(cfg)
    new_cfg["providers"] = new_providers

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = p.with_name(p.name + f".bak.{ts}")
    shutil.copy2(p, backup)
    _atomic_write(p, new_cfg)
    print(f"Migrated {p}. Backup at {backup}.")
    return MIGRATED


def main(argv: list[str]) -> int:
    """Entry point for ``python -m job_finder.migrate_config``."""
    if len(argv) > 1:
        target = Path(argv[1])
    else:
        # Lazy import: avoid pulling Flask machinery during tests that point
        # at an explicit path.
        from job_finder.web import user_data_dirs

        target = user_data_dirs.config_path()

    if not target.exists():
        print(f"No config found at {target}; nothing to migrate.")
        return 0

    status = migrate_file(target)
    return 0 if status in (MIGRATED, ALREADY_MIGRATED) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
