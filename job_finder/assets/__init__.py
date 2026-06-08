"""Bundled binary assets (tray icon, etc.).

Exists so ``importlib.resources.files("job_finder.assets")`` can resolve the
package at runtime. Hatchling auto-includes everything under ``job_finder/``
into the wheel (see ``pyproject.toml`` ``packages = ["job_finder"]``), so no
separate package-data declaration is needed.
"""
