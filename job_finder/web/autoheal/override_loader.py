"""Override loader — load, validate, and cache declarative JSON override recipes.

Provides a module-level singleton (``_LOADER``) that reads recipe files from
``<userdata>/heal_overrides/<surface>/<source>.json``.  All public APIs return
``None`` (never raise) when a file is absent or invalid — a bad override must
never crash ingestion.

Source-key → file layout:
  - Email:   ``heal_overrides/email/<label>.json``      (label = SENDER_LABEL value)
  - ATS:     ``heal_overrides/ats/<platform>.json``     (platform = source key without "ats:" prefix)
  - Careers: ``heal_overrides/careers/<hostname>.json`` (hostname = source key without
    "careers:" prefix; filesystem-safe by construction — I5: no port, no colon)

Shipped defaults (D5): the same layout under the packaged
``job_finder/data/default_overrides/`` is scanned FIRST; user files override
defaults on key collision. A user-root tombstone ``<file_key>.disabled``
suppresses the shipped default for that key — this keeps rollback fully
effective against a garbage-yielding default (which cannot be unlinked from
site-packages). The tombstone only masks the DEFAULT, never a user file.

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
        overrides_root: Root directory for user override files.  In production
            this is ``<userdata>/heal_overrides``; in tests use ``tmp_path``.
        defaults_root: Root directory for shipped default recipes.  In
            production this is the packaged ``job_finder/data/default_overrides``.
    """

    def __init__(
        self, overrides_root: Path | None = None, defaults_root: Path | None = None
    ) -> None:
        if overrides_root is None:
            from job_finder.web.user_data_dirs import user_data_root

            overrides_root = user_data_root() / "heal_overrides"
        if defaults_root is None:
            import job_finder.data as _data

            defaults_root = Path(_data.__file__).parent / "default_overrides"
        self._root = Path(overrides_root)
        self._defaults_root = Path(defaults_root)
        self._cache: _Cache = {"email": {}, "ats": {}, "careers": {}}
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

    def careers_recipe(self, source: str) -> HtmlRecipe | None:
        """Return the validated careers HtmlRecipe for *source*, or None.

        *source* should include the ``careers:`` prefix (e.g. ``"careers:acme.com"``).
        """
        return self._cache["careers"].get(source)  # type: ignore[return-value]

    def recipe_for(self, source: str) -> HtmlRecipe | AtsAliasRecipe | None:
        """Return the cached recipe for *source* on any surface, or None."""
        from job_finder.web.autoheal import surface_for_source

        surface = surface_for_source(source)
        return self._cache.get(surface, {}).get(source)

    def delete_override(self, surface: str, file_key: str) -> bool:
        """Suppress the effective override for *file_key*. True when anything was suppressed.

        Deletes the user ``.json`` if present; if a shipped default would
        still be effective afterwards (default file exists, not already
        tombstoned), writes the ``<file_key>.disabled`` tombstone. Never
        raises: missing files → False; OSError → logged, False.
        """
        suppressed = False
        path = self._override_dir(surface) / f"{file_key}.json"
        if path.is_file():
            try:
                path.unlink()
                suppressed = True
            except OSError:
                logger.exception("override_loader: failed to delete %s", path)
                return False

        default_path = self._defaults_root / surface / f"{file_key}.json"
        tombstone = self._override_dir(surface) / f"{file_key}.disabled"
        if default_path.is_file() and not tombstone.exists():
            try:
                tombstone.parent.mkdir(parents=True, exist_ok=True)
                tombstone.write_text("", encoding="utf-8")
                suppressed = True
            except OSError:
                logger.exception("override_loader: failed to write tombstone %s", tombstone)
                return False
        return suppressed

    def reload(self) -> None:
        """Re-scan defaults + overrides directories and swap the cache atomically."""
        new_cache: _Cache = {"email": {}, "ats": {}, "careers": {}}
        for surface in ("email", "ats", "careers"):
            self._scan_surface(new_cache, surface)
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
        for surface in ("email", "ats", "careers"):
            self._scan_surface(self._cache, surface)

    @staticmethod
    def _source_key(surface: str, file_key: str) -> str:
        """File stem → cache key: prefixed surfaces re-add their prefix."""
        if surface == "ats":
            return f"ats:{file_key}"
        if surface == "careers":
            return f"careers:{file_key}"
        return file_key

    def _scan_surface(self, cache: _Cache, surface: str) -> None:
        """Scan one surface: shipped defaults first, user files over the top.

        A user-root ``<file_key>.disabled`` tombstone suppresses the shipped
        default for that key; user ``.json`` files load regardless (a user
        override outranks both the default and the tombstone).
        """
        user_dir = self._override_dir(surface)
        defaults_dir = self._defaults_root / surface
        if defaults_dir.is_dir():
            for json_file in defaults_dir.glob("*.json"):
                if (user_dir / f"{json_file.stem}.disabled").exists():
                    continue  # tombstoned default
                source_key = self._source_key(surface, json_file.stem)
                recipe = self._load_file(json_file, surface, source_key)
                if recipe is not None:
                    cache[surface][source_key] = recipe
        if user_dir.is_dir():
            for json_file in user_dir.glob("*.json"):
                source_key = self._source_key(surface, json_file.stem)
                recipe = self._load_file(json_file, surface, source_key)
                if recipe is not None:
                    cache[surface][source_key] = recipe

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


def careers_recipe(source: str) -> HtmlRecipe | None:
    """Return the cached careers HtmlRecipe for *source*, or None."""
    return _get_loader().careers_recipe(source)


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
