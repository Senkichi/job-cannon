"""One-shot manual agentic backfill — runs the same logic as the nightly job.

Usage:
    uv run python scripts/run_agentic_backfill_oneshot.py [limit]
"""

import logging
import sys
import time
from pathlib import Path

# Project root is the parent of scripts/. When run as `python scripts/foo.py`
# sys.path[0] is the scripts dir, not the repo root, so `import job_finder` fails.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

from job_finder.config import load_config
from job_finder.web.agentic_enricher import run_agentic_backfill

config = load_config()
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50

t0 = time.time()
print(f"[agentic backfill] starting limit={limit}", file=sys.stderr, flush=True)
n = run_agentic_backfill("jobs.db", config, limit=limit)
elapsed = time.time() - t0
print(
    f"[agentic backfill] enriched={n} elapsed={elapsed:.1f}s",
    file=sys.stderr,
    flush=True,
)
