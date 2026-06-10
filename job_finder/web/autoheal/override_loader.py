"""Override loader — load, validate, and cache declarative JSON override recipes.

Provides a module-level singleton (``_LOADER``) that reads recipe files from
``<userdata>/heal_overrides/<surface>/<source>.json``.  All public APIs return
``None`` (never raise) when a file is absent or invalid — a bad override must
never crash ingestion.

Source-key → file layout:
  - Email:  ``heal_overrides/email/<label>.json``   (label = SENDER_LABEL value)
  - ATS:    ``heal_overrides/ats/<platform>.json``  (platform = source key without "ats:" prefix)

``reload()`` atomically swaps the in-memory cache by replacing the dict reference;
snapshot semantics guarantee that a reference captured before reload remains valid.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from job_finder.web.autoheal.recipe_schema import (
    AtsAliasRecipe,
    HtmlRecipe,
    validate_recipe,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OverrideLoader
# ---------------------------------------------------------------------------

# Cache type: surface → source-key → recipe
_Cache = dict[str, dict[str, HtmlRecipe | AtsAliasRecipe]]


class OverrideLoader:
    """Load, validate, and cache declarative recipe overrides from disk.

    Args:
        overrides_root: Root directory for override files.  In production this
            is ``<userdata>/heal_overrides``; in tests use ``tmp_path``.
    """

    def __init__(self, overrides_root: Path | None = None) -> None:
        if overrides_root is None:
            from job_finder.web.user_data_dirs import user_data_root

            overrides_root = user_data_root() / "heal_overrides"
        self._root = Path(overrides_root)
        self._cache: _Cache = {"email": {}, "ats": {}}
        self._load_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def html_recipe(self, source: str) -> HtmlRecipe | None:
        """Return the validated HtmlRecipe for *source*, or None if absent/invalid."""
        return self._cache["email"].get(source)  # type: ignore[return-value]

    def ats_alias(self, source: str) -> AtsAliasRecipe | None:
        """Return the validated AtsAliasRecipe for *source*, or None if absent/invalid.

        *source* should include the ``ats:`` prefix (e.g. ``"ats:lever"``).
        """
        return self._cache["ats"].get(source)  # type: ignore[return-value]

    def recipe_for(self, source: str) -> HtmlRecipe | AtsAliasRecipe | None:
        """Return the cached recipe for *source* on any surface, or None.

        Until D4 adds the ``careers`` cache surface, careers sources resolve
        against an empty dict → None, which is correct (no careers overrides
        can exist yet).
        """
        from job_finder.web.autoheal import surface_for_source

        surface = surface_for_source(source)
        return self._cache.get(surface, {}).get(source)

    def delete_override(self, surface: str, file_key: str) -> bool:
        """Suppress the user override file if present. Returns True when removed.

        Never raises: missing file → False; OSError → logged, False. (D5
        extends this contract: when a SHIPPED default exists for the key,
        suppression writes a user-root tombstone and still returns True.)
        """
        path = self._override_dir(surface) / f"{file_key}.json"
        if not path.is_file():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            logger.exception("override_loader: failed to delete %s", path)
            return False

    def reload(self) -> None:
        """Re-scan the overrides directory and swap the cache atomically."""
        new_cache: _Cache = {"email": {}, "ats": {}}
        self._scan_surface(new_cache, "email")
        self._scan_surface_ats(new_cache)
        # Atomic swap — no mutation of the old dict
        self._cache = new_cache

    def write_override(self, surface: str, source: str, recipe_dict: dict) -> None:
        """Validate *recipe_dict* then write it atomically to the override file.

        Raises:
            ValueError: If *recipe_dict* fails ``validate_recipe()`` validation.
            Exception: If JSON serialisation or file I/O fails.
        """
        # Validate first — this raises ValueError on schema failure
        validate_recipe(surface, recipe_dict)

        out_dir = self._override_dir(surface)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{source}.json"

        # Write via temp file + os.replace for atomicity
        fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(recipe_dict, fh, indent=2)
            os.replace(tmp_path, out_path)
        except Exception:
            # Clean up temp file if replace didn't happen
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        self._scan_surface(self._cache, "email")
        self._scan_surface_ats(self._cache)

    def _scan_surface(self, cache: _Cache, surface: str) -> None:
        surface_dir = self._override_dir(surface)
        if not surface_dir.is_dir():
            return
        for json_file in surface_dir.glob("*.json"):
            source_key = json_file.stem
            recipe = self._load_file(json_file, surface, source_key)
            if recipe is not None:
                cache[surface][source_key] = recipe

    def _scan_surface_ats(self, cache: _Cache) -> None:
        """Scan the ats/ directory; source keys use the ``ats:`` prefix convention."""
        surface_dir = self._override_dir("ats")
        if not surface_dir.is_dir():
            return
        for json_file in surface_dir.glob("*.json"):
            platform = json_file.stem
            source_key = f"ats:{platform}"
            recipe = self._load_file(json_file, "ats", source_key)
            if recipe is not None:
                cache["ats"][source_key] = recipe

    def _load_file(
        self, path: Path, surface: str, source_key: str
    ) -> HtmlRecipe | AtsAliasRecipe | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "override_loader: failed to parse %s (%s); skipping override for '%s'",
                path.name,
                exc,
                source_key,
            )
            return None
        try:
            return validate_recipe(surface, data)
        except ValueError as exc:
            logger.warning(
                "override_loader: invalid recipe in %s: %s; skipping override for '%s'",
                path.name,
                exc,
                source_key,
            )
            return None

    def _override_dir(self, surface: str) -> Path:
        return self._root / surface


# ---------------------------------------------------------------------------
# Module-level singleton (production path)
# ---------------------------------------------------------------------------

_LOADER: OverrideLoader | None = None


def _get_loader() -> OverrideLoader:
    global _LOADER
    if _LOADER is None:
        _LOADER = OverrideLoader()
    return _LOADER


def html_recipe(source: str) -> HtmlRecipe | None:
    """Return the cached HtmlRecipe for *source*, or None."""
    return _get_loader().html_recipe(source)


def ats_alias(source: str) -> AtsAliasRecipe | None:
    """Return the cached AtsAliasRecipe for *source*, or None."""
    return _get_loader().ats_alias(source)


def reload() -> None:
    """Re-scan overrides and hot-swap the singleton cache."""
    _get_loader().reload()


def write_override(surface: str, source: str, recipe_dict: dict) -> None:
    """Write an override file atomically via the singleton loader."""
    _get_loader().write_override(surface, source, recipe_dict)


def recipe_for(source: str) -> HtmlRecipe | AtsAliasRecipe | None:
    """Return the cached recipe for *source* on any surface, or None."""
    return _get_loader().recipe_for(source)


def delete_override(surface: str, file_key: str) -> bool:
    """Suppress the override file for *file_key* via the singleton loader."""
    return _get_loader().delete_override(surface, file_key)
