"""SQLite persistence layer for job deduplication and run history.

Package layout:

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
- ``_pipeline_queries.py`` — pipeline-detection read queries
  (``get_pending_detections``, ``get_pipeline_events``, ``resolve_detection``).
- ``_dashboard_queries.py`` — dashboard / read-side aggregates
  (``get_dashboard_stats``, ``get_jobs_by_status``, ``get_pipeline_summary``,
  ``get_recent_activity``, ``get_recent_pipeline_events``, ``get_recent_runs``,
  ``get_distinct_locations``).

This ``__init__.py`` is now lifecycle-and-re-exports only — no module-level
functions live here. ``from job_finder.db import X`` continues to be the
canonical import path for every public name in the package.

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
from ._dashboard_queries import get_dashboard_stats as get_dashboard_stats
from ._dashboard_queries import get_distinct_locations as get_distinct_locations
from ._dashboard_queries import get_jobs_by_status as get_jobs_by_status
from ._dashboard_queries import get_pipeline_summary as get_pipeline_summary
from ._dashboard_queries import get_recent_activity as get_recent_activity
from ._dashboard_queries import get_recent_pipeline_events as get_recent_pipeline_events
from ._dashboard_queries import get_recent_runs as get_recent_runs

# Job CRUD + the JOBS_ALL_COLUMNS projection.
from ._jobs import JOBS_ALL_COLUMNS as JOBS_ALL_COLUMNS
from ._jobs import IngestionRejected as IngestionRejected
from ._jobs import UpsertResult as UpsertResult
from ._jobs import get_job as get_job
from ._jobs import load_job_context as load_job_context
from ._jobs import merge_description as merge_description
from ._jobs import upsert_job as upsert_job

# DB write paths — runs log + per-row persistence + pipeline state machine.
from ._persistence import log_run as log_run
from ._persistence import persist_job_archetype as persist_job_archetype
from ._persistence import persist_job_assessment as persist_job_assessment
from ._persistence import persist_job_expiry_state as persist_job_expiry_state
from ._persistence import update_pipeline_status as update_pipeline_status

# Pipeline-detection + dashboard read queries (formerly job_finder/db_pipeline.py
# + db_queries.py at the package root; moved into db/ in the polish-review
# 2026-05-26 sweep, see ``_pipeline_queries.py`` + ``_dashboard_queries.py``).
from ._pipeline_queries import get_pending_detections as get_pending_detections
from ._pipeline_queries import get_pipeline_events as get_pipeline_events
from ._pipeline_queries import resolve_detection as resolve_detection

# Read-only filter queries — sort_by allowlist invariant lives here.
from ._queries import get_distinct_country_codes as get_distinct_country_codes
from ._queries import get_distinct_sources as get_distinct_sources
from ._queries import get_distinct_workplace_types as get_distinct_workplace_types
from ._queries import get_filtered_jobs as get_filtered_jobs
