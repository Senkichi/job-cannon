"""SQLite persistence layer for job deduplication and run history.

Package layout (post-S7d):

- ``_classification.py`` — JobAssessment + derive_classification +
  ``_SUB_SCORE_KEYS`` (pure scoring-rule logic, zero DB deps).
- ``_persistence.py`` — write paths (``persist_*``,
  ``update_pipeline_status``, ``log_run``).
- ``_jobs.py`` — job CRUD (``upsert_job``, ``get_job``, ``merge_description``,
  ``load_job_context``) and the canonical ``JOBS_ALL_COLUMNS`` projection.
- ``_queries.py`` — read-only filters (``get_filtered_jobs``,
  ``get_distinct_sources``). The sort_by allowlist + the f-string composer
  that consumes it MUST stay co-located inside ``_queries.py`` (S7d
  security invariant — see CLAUDE.md and the comment on ``allowed_sort_cols``
  inside ``get_filtered_jobs``).

This ``__init__.py`` is now lifecycle-and-re-exports only — no module-level
functions live here. Sibling modules ``job_finder/db_pipeline.py`` and
``job_finder/db_queries.py`` are NOT inside this package; they remain at the
``job_finder/`` level and their public functions are re-exported below so
existing ``from job_finder.db import X`` paths keep working unchanged.

Dual-path note (CLI-era / web-era): this module is the original CLI-era DB
layer (module-level functions accept a ``sqlite3.Connection`` directly).
``job_finder/web/db_helpers.py`` is the web-era per-request ``g.db`` pattern;
the two coexist by design and S7d does NOT collapse them.
"""

from __future__ import annotations

# v3.0 scoring-rule cluster — pure logic, no DB deps.
# PEP 484 explicit re-export form (`as X`) documents the contract and
# silences pyright's reportUnusedImport.
from ._classification import _SUB_SCORE_KEYS as _SUB_SCORE_KEYS
from ._classification import JobAssessment as JobAssessment
from ._classification import derive_classification as derive_classification

# DB write paths — runs log + per-row persistence + pipeline state machine.
from ._persistence import log_run as log_run
from ._persistence import persist_job_archetype as persist_job_archetype
from ._persistence import persist_job_assessment as persist_job_assessment
from ._persistence import persist_job_expiry_state as persist_job_expiry_state
from ._persistence import update_pipeline_status as update_pipeline_status

# Job CRUD + the JOBS_ALL_COLUMNS projection.
from ._jobs import JOBS_ALL_COLUMNS as JOBS_ALL_COLUMNS
from ._jobs import get_job as get_job
from ._jobs import load_job_context as load_job_context
from ._jobs import merge_description as merge_description
from ._jobs import upsert_job as upsert_job

# Read-only filter queries — sort_by allowlist invariant lives here.
from ._queries import get_distinct_sources as get_distinct_sources
from ._queries import get_filtered_jobs as get_filtered_jobs

# Sibling-module re-exports (job_finder/db_pipeline.py + db_queries.py).
from job_finder.db_pipeline import get_pending_detections as get_pending_detections
from job_finder.db_pipeline import get_pipeline_events as get_pipeline_events
from job_finder.db_pipeline import resolve_detection as resolve_detection
from job_finder.db_queries import get_dashboard_stats as get_dashboard_stats
from job_finder.db_queries import get_distinct_locations as get_distinct_locations
from job_finder.db_queries import get_jobs_by_status as get_jobs_by_status
from job_finder.db_queries import get_pipeline_summary as get_pipeline_summary
from job_finder.db_queries import get_recent_activity as get_recent_activity
from job_finder.db_queries import get_recent_pipeline_events as get_recent_pipeline_events
from job_finder.db_queries import get_recent_runs as get_recent_runs
