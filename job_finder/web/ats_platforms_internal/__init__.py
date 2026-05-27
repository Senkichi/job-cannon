"""ATS platform scanner registry (added in polish-review F1).

This package is the new home for per-platform scanner code. During F1 it
coexists with the flat ``job_finder/web/ats_platforms.py`` module, which
delegates to ``run_platform_scan`` for the 12 known ATS platforms.

The "internal" suffix is temporary — it avoids the file/package name clash
with ``ats_platforms.py`` during the two-commit F1 transition. A later
optional commit may rename to ``ats_platforms/`` (package-promote).
"""
