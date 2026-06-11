# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the frozen Windows build (WP9).

Build from the repo root:

    uv sync --extra packaging
    uv run pyinstaller packaging/windows/job-cannon.spec

Output: dist/JobCannon/ (onedir). Onedir over onefile deliberately:
faster startup (no self-extraction), friendlier antivirus behavior, and
the standard layout for installed desktop apps — Inno Setup packages the
directory (see installer.iss).

Relative paths in this file resolve against the spec's own directory
(PyInstaller semantics), so "launcher.py" and "job-cannon.ico" are
siblings of this spec regardless of the invocation cwd.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

# Templates, Jinja partials, config.example.yaml, tray_icon.png — everything
# non-.py inside the package. Flask resolves its template folder relative to
# the frozen module location, which collect_data_files preserves.
datas = collect_data_files("job_finder")

# importlib.metadata.version("job-cannon") drives --version and the
# update-check banner; without the dist-info the frozen app reports
# "0.0.0+dev" (__main__._get_version fallback).
datas += copy_metadata("job-cannon")

# keyring discovers backends through importlib.metadata entry points;
# bundling its metadata keeps that discovery working frozen.
datas += copy_metadata("keyring")

hiddenimports = [
    # The whole application package. Two dynamic-import surfaces make this
    # mandatory, not defensive: migrations are discovered via
    # pkgutil.iter_modules + importlib.import_module
    # (job_finder/web/migrations/__init__.py), and scoring prompt variants
    # load by name (job_finder/web/job_scorer.py::_load_variant). PyInstaller's
    # static analysis cannot see either; FrozenImporter supports
    # pkgutil.iter_modules only for modules that were actually bundled.
    *collect_submodules("job_finder"),
    # APScheduler triggers are imported directly today (_jobs.py), but 3.x
    # also resolves triggers via entry points in some code paths — keep both
    # pinned here so a refactor to string-named triggers can't break the
    # frozen build silently.
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    # keyring backend modules are imported lazily at first secret access;
    # static analysis never sees them.
    "keyring.backends.Windows",
    "keyring.backends.fail",
    # pystray picks its platform backend dynamically (pystray/_init_.py
    # reads the platform at import time).
    "pystray._win32",
]

excludes = [
    # Playwright is dev-extras only; runtime imports are lazy (Issue #291)
    # and AI-navigator features degrade gracefully without it — exactly the
    # pipx-without-playwright behavior.
    "playwright",
    # llama-cpp-python is the optional local-ai extra.
    "llama_cpp",
    # Eval extra + test tooling — never imported by the app.
    "pytest",
    "numpy",
    "scipy",
    # Pillow probes for tkinter ImageTk support; the app has no Tk UI.
    "tkinter",
]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# google-api-python-client's hook bundles the static discovery document for
# EVERY Google API — 581 JSON files, ~96 MB, more than a third of the whole
# bundle. The app builds exactly one client: build("gmail", "v1")
# (gmail_source.py, onboarding/gmail_test.py). Keep only the gmail documents.
_DISCOVERY_DIR = "googleapiclient" + "/discovery_cache/documents/"


def _keep_data(dest: str) -> bool:
    normalized = dest.replace("\\", "/")
    if _DISCOVERY_DIR not in normalized:
        return True
    return normalized.rsplit("/", 1)[-1].startswith("gmail.")


a.datas = [entry for entry in a.datas if _keep_data(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="job-cannon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed: tray mode is the default UX and logs already land in
    # %APPDATA%\JobCannon\logs\app.log. __main__._reconfigure_stdio_utf8
    # tolerates the windowed-mode null stdio (catches AttributeError), and
    # CPython's print() is a no-op when sys.stdout is None — no console
    # subsystem needed even for the banner prints.
    console=False,
    icon="job-cannon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JobCannon",
)
