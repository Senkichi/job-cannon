"""PyInstaller entry point for the frozen Windows build.

A thin wrapper so the spec file has a stable script target that is not the
package's ``__main__.py`` (PyInstaller treats ``-m``-style entry points
awkwardly; a plain script that imports the real ``main`` freezes cleanly).
All behavior — tray default, ``--terminal``, pidfile single-instance,
browser auto-open — lives in :func:`job_finder.__main__.main`.

multiprocessing.freeze_support() guards against the classic frozen-app
fork bomb: psutil and Werkzeug don't spawn workers today, but any future
``multiprocessing`` use inside a frozen windowed exe re-executes the
binary, and without this call each re-execution would boot a whole new
Job Cannon instead of the worker bootstrap.
"""

import multiprocessing

from job_finder.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
