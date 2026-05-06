"""Backward-compat entry point. Prefer ``uv run job-cannon`` or ``python -m job_finder``.

Kept so anyone with a bookmarked ``python run.py`` invocation still works.
The real entry point is :func:`job_finder.__main__.main`.
"""

from job_finder.__main__ import main

if __name__ == "__main__":
    main()
