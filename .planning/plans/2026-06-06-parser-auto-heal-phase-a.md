# Parser Auto-Heal — Phase A Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add zero-behavior-change observability that captures a scrubbed rolling corpus of every parser's real input/output and flags a source as `DEGRADED` the moment it structurally breaks, surfaced on the dashboard.

**Architecture:** Two new SQLite tables (`corpus_sample` ring buffer + `source_health` state) written by a never-raising `recorder` that the three ingestion surfaces (email, ATS, careers) call after each extraction. A post-ingestion detection pass promotes a source to `DEGRADED` after 3 consecutive baseline-violating zero-yields. A dashboard widget reads `source_health`. No parse logic changes; capture is additive and failure-isolated.

**Tech Stack:** Python 3.13, Flask, raw SQLite (WAL), Jinja2 + jinja2-fragments, HTMX, pytest.

**Source of truth:** `.planning/specs/2026-06-06-parser-auto-heal-design.md` (Phase A = §11 row A).

---

## Scope & decisions (read before starting)

- **Phase A only.** No `Strategy` abstraction, no heal pipeline, no LLM. Those are Phases B–D, separately planned.
- **Storage:** corpus samples live in SQLite (single-user scale; matches the project's raw-SQL/no-ORM convention), not on the filesystem.
- **Break rule (deterministic, no parser-internal changes):** a source with `baseline_yield >= 1` that returns **0 jobs** on a **meaningful** input (`len(raw_text) >= MIN_MEANINGFUL_LEN`, default 200) increments `consecutive_breaks`. Any input yielding ≥1 job resets it to 0. At `consecutive_breaks >= BREAK_THRESHOLD` (default 3) the source flips to `DEGRADED`. This is the Phase-A realization of the spec's "≥3 baseline-violating inputs in a rolling window" — implemented as *consecutive* zeros + a size gate, which is stricter and avoids counting genuine-empty inboxes. (Spec deviation, intentional, documented here.)
- **Source granularity:** email = per parser **canonical label** (`linkedin`, `glassdoor`, …) via an explicit address→label map (raw `SENDER_PARSERS` keys are full addresses, and LinkedIn has two addresses → one label); ATS = per platform (`ats:greenhouse`, …); careers = `careers` aggregate (per-company crawler health already lives in `company_scan_log`).
- **Detection is email-only in Phase A (honest scope).** The break rule needs the *raw input* captured so "large input + zero output" is distinguishable from "genuinely empty." Email captures the real (scrubbed) body, so detection works. ATS/careers only have their *post-filter output* reachable at the hook sites; capturing the raw API response / full HTML is the Phase-B refinement. So in Phase A, ATS + careers **capture samples for baseline only** (`detect=False`) and do **not** auto-degrade. The dashboard surfaces email breaks now; ATS/careers detection lights up in Phase B when raw-artifact capture lands. This is a deliberate, documented limitation — not a gap to paper over.
- **Surfaces capture in 4 code sites** (no shared chokepoint exists): `gmail_source.py`, `imap_source.py` (email, `detect=True`), `ats_scanner/_run.py`, `careers_crawler/_persistence.py` (`detect=False`).

## File structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `job_finder/web/migrations/m084_parser_health.py` | Create | `corpus_sample` + `source_health` tables. |
| `job_finder/sources/_pii_scrub.py` | Create | Reusable PII scrubber (deny-list from config + identity); seeded from the test's rules. |
| `job_finder/web/autoheal/__init__.py` | Create | Package marker + constants (`MIN_MEANINGFUL_LEN`, `BREAK_THRESHOLD`, `BASELINE_WINDOW`). |
| `job_finder/web/autoheal/corpus_store.py` | Create | `append_sample` (scrub+insert+evict), `recent_samples`, `baseline_yield`. |
| `job_finder/web/autoheal/health_monitor.py` | Create | `record_extraction` (append + update health), `run_detection`, `degraded_sources`. |
| `job_finder/web/activity_tracker.py` | Modify | Add `ACTION_SOURCE_DEGRADED`. |
| `job_finder/sources/gmail_source.py` | Modify | Accumulate `extraction_records` (mirror `parse_failures`) + `SENDER_LABEL` map. |
| `job_finder/sources/imap_source.py` | Modify | Same accumulation in the IMAP loop (using the shared `SENDER_LABEL`). |
| `job_finder/web/ingestion_runner.py` | Modify | Drain email `extraction_records` (Gmail inline in `_fetch_gmail`; IMAP via a `post_extract` hook on `_run_simple_source`). |
| `job_finder/web/ats_scanner/_run.py` | Modify | Call `record_extraction(..., detect=False)` after `company_jobs_found`. |
| `job_finder/web/careers_crawler/_persistence.py` | Modify | Call `record_extraction(..., detect=False)` inside `_upsert_and_log`. |
| `job_finder/web/pipeline_runner.py` | Modify | Run detection pass post-ingestion (after the DB block closes). |
| `job_finder/web/blueprints/dashboard.py` | Modify | `_get_degraded_sources_context` + fragment route + index kwargs. |
| `job_finder/web/templates/dashboard/_degraded_sources.html` | Create | Widget partial (badge-pill style from `dashboard/_dashboard_history.html`). |
| `job_finder/web/templates/dashboard/index.html` | Modify | Include widget in an `hx-get` wrapper. |
| `tests/test_autoheal_*.py` | Create | Unit + route + integration tests. |
| `tests/test_imap_parser_roundtrip.py` | Modify | Import deny-list from `_pii_scrub` (no divergence). |

---

## Chunk 1: Foundations (schema, scrubber, corpus store, health monitor)

### Task 1: Migration m084 — health tables

**Files:**
- Create: `job_finder/web/migrations/m084_parser_health.py`
- Test: `tests/test_autoheal_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_autoheal_migration.py
import sqlite3
from job_finder.web.db_migrate import run_migrations


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_m084_creates_health_tables(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    assert _columns(conn, "corpus_sample") >= {
        "id", "source", "surface", "raw_text", "output_json", "captured_at",
    }
    assert _columns(conn, "source_health") >= {
        "source", "surface", "status", "consecutive_breaks",
        "baseline_yield", "last_signal", "last_break_at", "updated_at",
    }
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 84
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_migration.py -q`
Expected: FAIL — `no such table: corpus_sample`.

- [ ] **Step 3: Write the migration**

```python
# job_finder/web/migrations/m084_parser_health.py
"""Migration 84 — parser auto-heal Phase A: corpus_sample + source_health.

corpus_sample is a per-source rolling buffer of PII-scrubbed raw extractor
inputs plus a snapshot of what the live extractor produced. source_health holds
one current-state row per source for the dashboard DEGRADED surface. Both are
pure observability; nothing reads them in the parse hot path.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=84,
    description="parser auto-heal Phase A: corpus_sample + source_health tables",
    sql=[
        """CREATE TABLE IF NOT EXISTS corpus_sample (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            surface TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            output_json TEXT NOT NULL DEFAULT '{}',
            captured_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_corpus_sample_source ON corpus_sample(source, captured_at DESC)",
        """CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            surface TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'healthy',
            consecutive_breaks INTEGER NOT NULL DEFAULT 0,
            baseline_yield REAL NOT NULL DEFAULT 0,
            last_signal TEXT DEFAULT NULL,
            last_break_at TEXT DEFAULT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_source_health_status ON source_health(status)",
    ],
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --active pytest tests/test_autoheal_migration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/migrations/m084_parser_health.py tests/test_autoheal_migration.py
git commit -m "feat(autoheal): m084 corpus_sample + source_health tables"
```

---

### Task 2: PII scrubber

**Files:**
- Create: `job_finder/sources/_pii_scrub.py`
- Test: `tests/test_pii_scrub.py`
- Modify: `tests/test_imap_parser_roundtrip.py` (import deny-list)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pii_scrub.py
from job_finder.sources._pii_scrub import scrub_text, DEFAULT_DENYLIST


def test_removes_to_header_lines():
    raw = "From: jobs@x.com\nTo: senki@example.com\nSubject: hi\nBody here"
    out = scrub_text(raw)
    assert "To: senki@example.com" not in out
    assert "Body here" in out


def test_redacts_identifiers_case_insensitively():
    out = scrub_text("Hello Senki and SENKICHI", identifiers=["senki", "senkichi"])
    assert "senki" not in out.lower()
    assert "[redacted]" in out.lower()


def test_redacts_bare_emails():
    out = scrub_text("reach me at jane.doe@gmail.com please")
    assert "jane.doe@gmail.com" not in out
    assert "please" in out


def test_default_denylist_is_iterable_of_str():
    assert all(isinstance(x, str) for x in DEFAULT_DENYLIST)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --active pytest tests/test_pii_scrub.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the scrubber**

```python
# job_finder/sources/_pii_scrub.py
"""Reusable PII scrubbing for captured parser inputs.

Phase A of parser auto-heal stores real email/HTML/JSON the parsers saw. This
strips obvious personal data BEFORE anything is written to disk. The deny-list
seeds from the same rules the fixture-PII test enforces, plus any caller-supplied
identifiers (the local user's name/email from config), so a public multi-user
release scrubs each user's own identity rather than a hardcoded one.
"""

from __future__ import annotations

import re

# Seed identifiers (kept in sync with tests/test_imap_parser_roundtrip.py).
DEFAULT_DENYLIST: tuple[str, ...] = ("senki", "senkichi", "@users.noreply.github.com")

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TO_HEADER_RE = re.compile(r"^\s*(to|cc|bcc|delivered-to|x-original-to)\s*:.*$", re.IGNORECASE)
_REDACTED = "[redacted]"


def scrub_text(text: str, identifiers: tuple[str, ...] | list[str] | None = None) -> str:
    """Return *text* with recipient headers dropped and PII redacted.

    Idempotent and never raises on str input. ``identifiers`` extends (does not
    replace) DEFAULT_DENYLIST — pass the local user's name/email from config.
    """
    if not text:
        return text or ""
    deny = tuple(DEFAULT_DENYLIST) + tuple(identifiers or ())

    kept = [ln for ln in text.splitlines() if not _TO_HEADER_RE.match(ln)]
    out = "\n".join(kept)

    out = _EMAIL_RE.sub(_REDACTED, out)
    for ident in deny:
        if not ident:
            continue
        out = re.sub(re.escape(ident), _REDACTED, out, flags=re.IGNORECASE)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --active pytest tests/test_pii_scrub.py -q`
Expected: PASS.

- [ ] **Step 5: De-duplicate the deny-list in the fixture test**

In `tests/test_imap_parser_roundtrip.py`, replace the inline `denylist = [...]` (around line 132) with an import so the two never diverge:

```python
from job_finder.sources._pii_scrub import DEFAULT_DENYLIST
...
        denylist = list(DEFAULT_DENYLIST)
```

- [ ] **Step 6: Run both test files**

Run: `uv run --active pytest tests/test_pii_scrub.py tests/test_imap_parser_roundtrip.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add job_finder/sources/_pii_scrub.py tests/test_pii_scrub.py tests/test_imap_parser_roundtrip.py
git commit -m "feat(autoheal): reusable PII scrubber + share deny-list with fixture test"
```

---

### Task 3: autoheal package + CorpusStore

**Files:**
- Create: `job_finder/web/autoheal/__init__.py`
- Create: `job_finder/web/autoheal/corpus_store.py`
- Test: `tests/test_autoheal_corpus_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_autoheal_corpus_store.py
import sqlite3
from job_finder.web.db_migrate import run_migrations
from job_finder.web.autoheal import corpus_store


def _conn(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_append_inserts_scrubbed_sample(tmp_path):
    conn = _conn(tmp_path)
    corpus_store.append_sample(conn, "linkedin", "email",
                               "To: senki@x.com\nSoftware Engineer", {"job_count": 1})
    row = conn.execute("SELECT raw_text, output_json FROM corpus_sample").fetchone()
    assert "To: senki@x.com" not in row["raw_text"]
    assert '"job_count": 1' in row["output_json"]


def test_ring_buffer_evicts_oldest(tmp_path):
    conn = _conn(tmp_path)
    for i in range(corpus_store.MAX_SAMPLES_PER_SOURCE + 5):
        corpus_store.append_sample(conn, "linkedin", "email", f"body {i}", {"job_count": 1})
    n = conn.execute("SELECT COUNT(*) FROM corpus_sample WHERE source='linkedin'").fetchone()[0]
    assert n == corpus_store.MAX_SAMPLES_PER_SOURCE


def test_baseline_yield_averages_recent_nonzero(tmp_path):
    conn = _conn(tmp_path)
    for c in (2, 4):
        corpus_store.append_sample(conn, "glassdoor", "email", "x" * 300, {"job_count": c})
    assert corpus_store.baseline_yield(conn, "glassdoor") == 3.0


def test_baseline_yield_zero_when_no_history(tmp_path):
    conn = _conn(tmp_path)
    assert corpus_store.baseline_yield(conn, "unknown") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_corpus_store.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the package constants + store**

```python
# job_finder/web/autoheal/__init__.py
"""Parser auto-heal — Phase A (observability only).

Captures a PII-scrubbed rolling corpus of real parser inputs/outputs and tracks
per-source health so a structural break surfaces on the dashboard. No heal, no
LLM in this phase. See .planning/specs/2026-06-06-parser-auto-heal-design.md.
"""

# Detection tuning (see plan "Break rule").
MIN_MEANINGFUL_LEN = 200   # inputs shorter than this never count as a break (meta/empty emails)
BREAK_THRESHOLD = 3        # consecutive baseline-violating zero-yields → DEGRADED
BASELINE_WINDOW = 20       # how many recent non-zero samples define baseline_yield
```

```python
# job_finder/web/autoheal/corpus_store.py
"""Rolling per-source corpus of scrubbed parser inputs + output snapshots."""

from __future__ import annotations

import json
import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.sources._pii_scrub import scrub_text
from job_finder.web.autoheal import BASELINE_WINDOW

MAX_SAMPLES_PER_SOURCE = 50


def append_sample(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    raw_text: str,
    output_snapshot: dict,
    *,
    scrub_identifiers: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Scrub *raw_text*, insert one sample, evict oldest beyond the cap. Commits."""
    scrubbed = scrub_text(raw_text or "", scrub_identifiers)
    conn.execute(
        "INSERT INTO corpus_sample (source, surface, raw_text, output_json, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, surface, scrubbed, json.dumps(output_snapshot), utc_now_iso()),
    )
    conn.execute(
        """DELETE FROM corpus_sample
           WHERE source = ? AND id NOT IN (
               SELECT id FROM corpus_sample WHERE source = ?
               ORDER BY id DESC LIMIT ?
           )""",
        (source, source, MAX_SAMPLES_PER_SOURCE),
    )
    conn.commit()


def baseline_yield(conn: sqlite3.Connection, source: str) -> float:
    """Mean job_count over the last BASELINE_WINDOW samples that produced ≥1 job.

    Zero when the source has no positive history — which means the break rule
    cannot fire (a source that never produced jobs can't 'break').
    """
    rows = conn.execute(
        "SELECT output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC LIMIT ?",
        (source, BASELINE_WINDOW),
    ).fetchall()
    counts = []
    for r in rows:
        try:
            c = int(json.loads(r[0]).get("job_count", 0))
        except (ValueError, TypeError):
            c = 0
        if c > 0:
            counts.append(c)
    return round(sum(counts) / len(counts), 2) if counts else 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --active pytest tests/test_autoheal_corpus_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/autoheal/__init__.py job_finder/web/autoheal/corpus_store.py tests/test_autoheal_corpus_store.py
git commit -m "feat(autoheal): CorpusStore ring buffer + baseline_yield"
```

---

### Task 4: HealthMonitor (record + detect + read)

**Files:**
- Create: `job_finder/web/autoheal/health_monitor.py`
- Modify: `job_finder/web/activity_tracker.py` (add action constant)
- Test: `tests/test_autoheal_health_monitor.py`

- [ ] **Step 1: Add the activity action constant**

In `job_finder/web/activity_tracker.py`, in the ACTION_* block (~line 41), add:

```python
ACTION_SOURCE_DEGRADED = "source_degraded"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_autoheal_health_monitor.py
import sqlite3
from job_finder.web.db_migrate import run_migrations
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal import BREAK_THRESHOLD


def _conn(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return str(db), c


def _establish_baseline(conn, source):
    for _ in range(3):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=2)


def test_record_creates_health_row(tmp_path):
    _, conn = _conn(tmp_path)
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=2)
    row = conn.execute("SELECT status, baseline_yield FROM source_health WHERE source='linkedin'").fetchone()
    assert row["status"] == "healthy"
    assert row["baseline_yield"] >= 1


def test_consecutive_zero_yields_flip_to_degraded(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    flagged = hm.run_detection(db)
    row = conn.execute("SELECT status, consecutive_breaks FROM source_health WHERE source='linkedin'").fetchone()
    assert row["status"] == "degraded"
    assert "linkedin" in flagged


def test_short_input_zero_does_not_count_as_break(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    hm.record_extraction(conn, "linkedin", "email", "tiny", job_count=0)  # below MIN_MEANINGFUL_LEN
    row = conn.execute("SELECT consecutive_breaks FROM source_health WHERE source='linkedin'").fetchone()
    assert row["consecutive_breaks"] == 0


def test_nonzero_yield_resets_break_counter(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=5)
    row = conn.execute("SELECT consecutive_breaks, status FROM source_health WHERE source='linkedin'").fetchone()
    assert row["consecutive_breaks"] == 0
    assert row["status"] == "healthy"


def test_no_baseline_never_breaks(tmp_path):
    db, conn = _conn(tmp_path)
    for _ in range(BREAK_THRESHOLD + 2):
        hm.record_extraction(conn, "neversource", "email", "x" * 400, job_count=0)
    row = conn.execute("SELECT status FROM source_health WHERE source='neversource'").fetchone()
    assert row["status"] == "healthy"


def test_degraded_sources_reader(tmp_path):
    db, conn = _conn(tmp_path)
    _establish_baseline(conn, "linkedin")
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)
    hm.run_detection(db)
    degraded = hm.degraded_sources(conn)
    assert any(d["source"] == "linkedin" for d in degraded)


def test_detect_false_captures_but_never_breaks(tmp_path):
    """ATS/careers capture path: baseline tracked, counter frozen at 0."""
    db, conn = _conn(tmp_path)
    for _ in range(3):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=5, detect=False)
    for _ in range(BREAK_THRESHOLD + 2):
        hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=0, detect=False)
    row = conn.execute(
        "SELECT status, consecutive_breaks, baseline_yield FROM source_health WHERE source='ats:greenhouse'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["consecutive_breaks"] == 0
    assert row["baseline_yield"] == 5.0   # baseline still recorded
    assert conn.execute("SELECT COUNT(*) FROM corpus_sample WHERE source='ats:greenhouse'").fetchone()[0] > 0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_health_monitor.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 4: Implement the monitor**

```python
# job_finder/web/autoheal/health_monitor.py
"""Per-source parse health: record extractions, detect breaks, read degraded set.

record_extraction is the single entry point the three ingestion surfaces call
after each extraction. It appends to the corpus and updates the running break
counter. It NEVER raises — observability must not break ingestion. run_detection
promotes counters that crossed the threshold to DEGRADED and logs an activity
row; it opens its own connection (background/orchestration caller).
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import BREAK_THRESHOLD, MIN_MEANINGFUL_LEN
from job_finder.web.autoheal import corpus_store
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def record_extraction(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    raw_text: str,
    job_count: int,
    *,
    scrub_identifiers=None,
    detect: bool = True,
) -> None:
    """Append a corpus sample and (when detect) update the break counter. Never raises.

    detect=False is capture-only: the corpus sample + baseline_yield are recorded
    but the break counter is frozen. ATS/careers use this in Phase A because only
    their post-filter output is reachable at the hook site — the raw API/HTML
    artifact needed for honest break detection is a Phase-B addition.
    """
    try:
        baseline = corpus_store.baseline_yield(conn, source)
        corpus_store.append_sample(
            conn, source, surface, raw_text, {"job_count": int(job_count)},
            scrub_identifiers=scrub_identifiers,
        )

        is_meaningful = len(raw_text or "") >= MIN_MEANINGFUL_LEN
        is_break = baseline >= 1 and int(job_count) == 0 and is_meaningful
        new_baseline = corpus_store.baseline_yield(conn, source)
        now = utc_now_iso()

        row = conn.execute(
            "SELECT consecutive_breaks FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        prior = row[0] if row else 0

        if not detect:
            consecutive = prior            # capture-only: baseline tracked, counter frozen
        elif int(job_count) > 0:
            consecutive = 0
        elif is_break:
            consecutive = prior + 1
        else:
            consecutive = prior

        conn.execute(
            """INSERT INTO source_health
                   (source, surface, status, consecutive_breaks, baseline_yield, updated_at)
               VALUES (?, ?, 'healthy', ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   surface = excluded.surface,
                   consecutive_breaks = excluded.consecutive_breaks,
                   baseline_yield = excluded.baseline_yield,
                   updated_at = excluded.updated_at,
                   status = CASE WHEN excluded.consecutive_breaks = 0
                                 THEN 'healthy' ELSE source_health.status END""",
            (source, surface, consecutive, new_baseline, now),
        )
        conn.commit()
    except Exception:  # observability must never break ingestion
        logger.exception("autoheal record_extraction failed for source=%s", source)


def run_detection(db_path: str) -> list[str]:
    """Flip any source whose counter reached threshold to DEGRADED. Returns names."""
    flagged: list[str] = []
    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT source, consecutive_breaks, status FROM source_health "
                "WHERE consecutive_breaks >= ?",
                (BREAK_THRESHOLD,),
            ).fetchall()
            now = utc_now_iso()
            for r in rows:
                if r["status"] != "degraded":
                    conn.execute(
                        "UPDATE source_health SET status='degraded', last_break_at=?, "
                        "last_signal=? WHERE source=?",
                        (now, f"{r['consecutive_breaks']} consecutive zero-yields", r["source"]),
                    )
                    flagged.append(r["source"])
            conn.commit()
    except Exception:
        logger.exception("autoheal run_detection failed")
        return flagged

    if flagged:
        from job_finder.web.activity_tracker import ACTION_SOURCE_DEGRADED, log_activity
        for src in flagged:
            log_activity(db_path, ACTION_SOURCE_DEGRADED, entity_id=src,
                         metadata={"reason": "consecutive_zero_yields", "threshold": BREAK_THRESHOLD})
            logger.warning("autoheal: source '%s' flagged DEGRADED", src)
    return flagged


def degraded_sources(conn: sqlite3.Connection) -> list[dict]:
    """All currently-degraded sources, most-recent break first (dashboard reader)."""
    rows = conn.execute(
        "SELECT source, surface, consecutive_breaks, baseline_yield, last_signal, last_break_at "
        "FROM source_health WHERE status='degraded' ORDER BY last_break_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --active pytest tests/test_autoheal_health_monitor.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/autoheal/health_monitor.py job_finder/web/activity_tracker.py tests/test_autoheal_health_monitor.py
git commit -m "feat(autoheal): HealthMonitor record/detect/read + source_degraded action"
```

---

> **CHUNK 1 REVIEW GATE** — dispatch plan-document-reviewer over Chunk 1 before implementing Chunk 2.

## Chunk 2: Wiring (capture hooks, detection seam, dashboard surface)

### Task 5: Email capture — sender labels + `extraction_records`

**Files:**
- Modify: `job_finder/sources/gmail_source.py` (`SENDER_LABEL` map; `__init__`; dispatch loop ~line 177)
- Modify: `job_finder/sources/imap_source.py` (`__init__`; dispatch loop ~line 115)
- Test: `tests/test_autoheal_email_capture.py`

**Why a label map:** `SENDER_PARSERS` keys are full email addresses, and LinkedIn maps **two** addresses to one parser. Health rows must be one-per-parser, so health keys on a canonical label, not the raw address. The drain plumbing itself gets its real unit test in Task 7 (`_record_email_extractions`); `GmailSource` can't be unit-constructed (its `__init__` runs OAuth), so Task 5 tests the pure label map.

- [ ] **Step 1: Add `SENDER_LABEL` beside `SENDER_PARSERS` in `gmail_source.py`**

```python
# job_finder/sources/gmail_source.py — next to SENDER_PARSERS
SENDER_LABEL: dict[str, str] = {
    "jobalerts-noreply@linkedin.com": "linkedin",
    "jobs-noreply@linkedin.com": "linkedin",
    "noreply@glassdoor.com": "glassdoor",
    "alert@indeed.com": "indeed",
    "donotreply@match.indeed.com": "indeed",
    "no-reply@ziprecruiter.com": "ziprecruiter",
    "monster@notifications.monster.com": "monster",
    "hello@trueup.io": "trueup",
    "no-reply@us.greenhouse-jobs.com": "greenhouse",
}
```

> Before writing, open `gmail_source.py` and copy the **exact** `SENDER_PARSERS` key strings — every key MUST have a `SENDER_LABEL` entry (Step 2 test enforces this).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_autoheal_email_capture.py
from job_finder.sources.gmail_source import SENDER_PARSERS, SENDER_LABEL


def test_every_sender_has_a_canonical_label():
    missing = [k for k in SENDER_PARSERS if k not in SENDER_LABEL]
    assert not missing, f"senders without a label: {missing}"


def test_linkedin_addresses_share_one_label():
    labels = {SENDER_LABEL[k] for k in SENDER_PARSERS if "linkedin" in k}
    assert labels == {"linkedin"}
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_email_capture.py -q`
Expected: FAIL — `SENDER_LABEL` undefined, or missing keys.

- [ ] **Step 4: Init `extraction_records` + append in both sources**

In `GmailSource.__init__` **and** `ImapSource.__init__`, add (mirroring `self.parse_failures`):

```python
self.extraction_records: list[dict] = []
```

Immediately after the `jobs = parser_fn(body, email_date)` dispatch line in each loop (`gmail_source.py` ~177; `imap_source.py` ~115), append:

```python
self.extraction_records.append({
    "label": SENDER_LABEL.get(sender, sender),  # loop var is `sender` in both files
    "raw_text": body,
    "job_count": len(jobs),
})
```

In `imap_source.py`, import the shared map: `from job_finder.sources.gmail_source import SENDER_LABEL` (gmail_source is already the registry home; imap already imports `SENDER_PARSERS` from it).

- [ ] **Step 5: Run the test + existing source suites for no regression**

Run: `uv run --active pytest tests/test_autoheal_email_capture.py tests/ -k "gmail or imap or source" -q`
Expected: PASS (capture is additive — no behavior change).

- [ ] **Step 6: Commit**

```bash
git add job_finder/sources/gmail_source.py job_finder/sources/imap_source.py tests/test_autoheal_email_capture.py
git commit -m "feat(autoheal): sender labels + per-email extraction_records"
```

---

### Task 6: ATS + careers capture hooks

**Files:**
- Modify: `job_finder/web/ats_scanner/_run.py` (~line 408, after `company_jobs_found`)
- Modify: `job_finder/web/careers_crawler/_persistence.py` (`_upsert_and_log`, ~line 80)
- Test: `tests/test_autoheal_scanner_capture.py`

Both hooks pass `detect=False` (capture-for-baseline only — see Scope: their raw artifact isn't reachable here, so honest break-detection is Phase B). Keep the artifacts **tiny** (a count summary, not big JSON) — there is no Phase-A value in storing the post-filter output, and it keeps the DB lean.

ATS hook — after `company_jobs_found = len(job_dicts)` (~`_run.py:408`), using the in-scope `conn`, `platform`, `slug`:

```python
from job_finder.web.autoheal.health_monitor import record_extraction
record_extraction(
    conn, f"ats:{platform}", "ats",
    f"matched={company_jobs_found} slug={slug}",   # baseline artifact only
    job_count=company_jobs_found,
    detect=False,
)
```

Careers hook — inside `_upsert_and_log` after `company_jobs_found`/`company_jobs_new` are computed (~`_persistence.py:80`), using the in-scope `ts_conn`, `tier_used`:

```python
from job_finder.web.autoheal.health_monitor import record_extraction
record_extraction(
    ts_conn, "careers", "careers",
    f"tier={tier_used} found={company_jobs_found}",
    job_count=company_jobs_found,
    detect=False,
)
```

> Place this **inside** the `with` block that owns `ts_conn` (before it closes) — `record_extraction` issues its own commit on that connection, which is fine, but the call must not land after `ts_conn` is dead.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_autoheal_scanner_capture.py
import sqlite3
from job_finder.web.db_migrate import run_migrations
from job_finder.web.autoheal import health_monitor as hm


def test_ats_source_naming_is_per_platform(tmp_path):
    db = str(tmp_path / "t.db"); run_migrations(db)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    hm.record_extraction(conn, "ats:greenhouse", "ats", "x" * 400, job_count=3)
    row = conn.execute("SELECT surface FROM source_health WHERE source='ats:greenhouse'").fetchone()
    assert row["surface"] == "ats"
```

- [ ] **Step 2: Run to verify it fails / passes appropriately**

Run: `uv run --active pytest tests/test_autoheal_scanner_capture.py -q`
Expected: PASS (record_extraction already exists; this pins the naming convention the hooks must use).

- [ ] **Step 3: Apply the two hook edits.**

- [ ] **Step 4: Run scanner + crawler suites for no regression**

Run: `uv run --active pytest tests/ -k "ats or scanner or careers or crawl" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/ats_scanner/_run.py job_finder/web/careers_crawler/_persistence.py tests/test_autoheal_scanner_capture.py
git commit -m "feat(autoheal): capture ATS + careers extractions into health monitor"
```

---

### Task 7: Drain email records + run detection

**Why this shape (verified against the code):** the Gmail/IMAP source objects are NOT reachable in `run_ingestion`. `_fetch_gmail(config, conn, summary)` holds the `GmailSource` and already drains `source.parse_failures` with `conn` live (`ingestion_runner.py:160-176`) — that's the Gmail seam. IMAP runs through the generic `_run_simple_source` (`ingestion_runner.py:224-269`) which hides the source and has no `conn`; we add an optional `post_extract` hook to that driver and pass a closure that opens its own connection with an **explicit** `db_path` (avoids the config-divergence hazard). The detection pass runs in `pipeline_runner.run_ingestion` at the post-DB seam (`pipeline_runner.py:223`), where `runner_conn`'s `with` block is already closed (line 208) so `run_detection` opening its own connection cannot self-lock.

**Files:**
- Modify: `job_finder/web/ingestion_runner.py` (helpers + Gmail drain + IMAP `post_extract` hook)
- Modify: `job_finder/web/pipeline_runner.py` (detection seam; pass `db_path` to `_fetch_imap`)
- Test: `tests/test_autoheal_email_drain.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_autoheal_email_drain.py
import sqlite3
import types
from job_finder.web.db_migrate import run_migrations
from job_finder.web.ingestion_runner import _record_email_extractions, _user_identifiers


def test_user_identifiers_from_config():
    cfg = {"sources": {"imap": {"email": "me@x.com"}}, "profile": {"name": "Jane Doe"}}
    assert _user_identifiers(cfg) == ("me@x.com", "Jane Doe")


def test_user_identifiers_empty_when_absent():
    assert _user_identifiers({}) == ()


def test_drain_records_each_extraction(tmp_path):
    db = str(tmp_path / "t.db"); run_migrations(db)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    fake = types.SimpleNamespace(extraction_records=[
        {"label": "linkedin", "raw_text": "x" * 400, "job_count": 3},
        {"label": "glassdoor", "raw_text": "y" * 400, "job_count": 0},
    ])
    _record_email_extractions(fake, conn, {})
    assert conn.execute("SELECT COUNT(*) FROM corpus_sample").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0] == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_email_drain.py -q`
Expected: FAIL — helpers do not exist.

- [ ] **Step 3: Add the helpers to `ingestion_runner.py`**

```python
def _user_identifiers(config: dict) -> tuple[str, ...]:
    """Personal identifiers to redact from captured email bodies, sourced from config.

    sources.imap.email is the verified real key (ingestion_runner.py:293). profile.name
    is included when present. Returns () when neither exists.
    """
    idents: list[str] = []
    email = config.get("sources", {}).get("imap", {}).get("email")
    if email:
        idents.append(email)
    name = config.get("profile", {}).get("name")
    if name:
        idents.append(name)
    return tuple(idents)


def _record_email_extractions(source, conn, config: dict) -> None:
    """Drain a source's accumulated extraction_records into the health monitor.

    Never raises (record_extraction swallows its own errors); observability must
    not break ingestion.
    """
    from job_finder.web.autoheal.health_monitor import record_extraction

    idents = _user_identifiers(config)
    for rec in getattr(source, "extraction_records", []):
        record_extraction(
            conn, rec["label"], "email", rec["raw_text"], rec["job_count"],
            scrub_identifiers=idents, detect=True,
        )
```

- [ ] **Step 4: Wire the Gmail drain** — in `_fetch_gmail`, immediately after the `for failure in getattr(source, "parse_failures", []):` loop ends (~line 176), add:

```python
        _record_email_extractions(source, conn, config)
```

- [ ] **Step 5: Wire the IMAP `post_extract` hook** — three small edits in `ingestion_runner.py`:

  (a) `_run_simple_source` gains an optional hook param and invokes it after extract:

```python
def _run_simple_source(spec: SourceSpec, config: dict, summary: dict, post_extract=None) -> list[Job]:
    ...
    try:
        source = spec.build_source(source_cfg, secret)
        jobs = spec.extract_jobs(source, source_cfg)
        if post_extract is not None:
            try:
                post_extract(source)
            except Exception:
                logger.exception("post_extract hook failed for %s", spec.name)
        jobs = _apply_title_gate(jobs, config, spec.name)
        ...
```

  (b) `_fetch_imap` takes `db_path` and passes a draining closure:

```python
def _fetch_imap(config: dict, summary: dict, db_path: str) -> list[Job]:
    def _drain(source):
        with standalone_connection(db_path) as c:
            _record_email_extractions(source, c, config)
    return _run_simple_source(_IMAP_SPEC, config, summary, post_extract=_drain)
```

  (Ensure `standalone_connection` is imported in `ingestion_runner.py`; add the import if absent.)

- [ ] **Step 6: Update the caller in `pipeline_runner.py`** — change `imap_jobs = _fetch_imap(config, summary)` (line 137) to:

```python
            imap_jobs = _fetch_imap(config, summary, db_path)
```

- [ ] **Step 7: Add the detection pass in `pipeline_runner.run_ingestion`** — after `summary["duration_seconds"] = ...` (line 223), before the final `logger.info`/`return`:

```python
    from job_finder.web.autoheal.health_monitor import run_detection
    summary["degraded_sources"] = run_detection(db_path)
```

- [ ] **Step 8: Run drain unit test + ingestion/pipeline suites**

Run: `uv run --active pytest tests/test_autoheal_email_drain.py tests/ -k "ingest or pipeline" -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add job_finder/web/ingestion_runner.py job_finder/web/pipeline_runner.py tests/test_autoheal_email_drain.py
git commit -m "feat(autoheal): drain email extractions (gmail+imap) + post-ingestion detection"
```

---

### Task 8: Dashboard DEGRADED widget

**Files:**
- Modify: `job_finder/web/blueprints/dashboard.py` (context builder + fragment route + index context)
- Create: `job_finder/web/templates/dashboard/_degraded_sources.html`
- Modify: `job_finder/web/templates/dashboard/index.html` (include wrapper)
- Test: `tests/test_autoheal_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_autoheal_dashboard.py
import sqlite3
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal import BREAK_THRESHOLD


def _degrade(conn, source="glassdoor"):
    for _ in range(3):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=2)
    for _ in range(BREAK_THRESHOLD):
        hm.record_extraction(conn, source, "email", "x" * 400, job_count=0)


def test_context_builder_lists_degraded(app):
    from job_finder.web.blueprints.dashboard import _get_degraded_sources_context
    from job_finder.web.db_helpers import get_db
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
        ctx = _get_degraded_sources_context(conn)
    assert any(d["source"] == "glassdoor" for d in ctx["degraded"])


def test_dashboard_shows_degraded_widget(client, app):
    from job_finder.web.db_helpers import get_db
    with app.app_context():
        conn = get_db(app.config["DB_PATH"])
        _degrade(conn)
        hm.run_detection(app.config["DB_PATH"])
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "glassdoor" in resp.data.decode()


def test_degraded_fragment_requires_htmx(client):
    resp = client.get("/dashboard/degraded-sources", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<html" not in body and "<!DOCTYPE" not in body


def test_healthy_system_shows_empty_state(client):
    resp = client.get("/dashboard/degraded-sources", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "All sources healthy" in resp.data.decode()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --active pytest tests/test_autoheal_dashboard.py -q`
Expected: FAIL — `_get_degraded_sources_context` / route missing.

- [ ] **Step 3: Add context builder + routes** in `job_finder/web/blueprints/dashboard.py`:

```python
from job_finder.web.autoheal.health_monitor import degraded_sources


def _get_degraded_sources_context(conn) -> dict:
    return {"degraded": degraded_sources(conn)}


@dashboard_bp.route("/degraded-sources", strict_slashes=False)
def degraded_sources_fragment():
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))
    conn = get_db()
    return render_template("dashboard/_degraded_sources.html",
                           **_get_degraded_sources_context(conn))
```

In `index()` (`dashboard.py:186`) there is **no `context` dict** — it passes context inline as `render_template("dashboard/index.html", **stats_ctx, **qa_ctx, recent_runs=..., ...)`. Add the degraded context to those kwargs (the existing `conn = get_db()` at line 173 is reused):

```python
    return render_template(
        "dashboard/index.html",
        **stats_ctx,
        **qa_ctx,
        **_get_degraded_sources_context(conn),   # <-- add this line
        recent_runs=recent_runs,
        user_activity=user_activity,
        pipeline_summary=pipeline_summary,
        pending_detections=pending_detections,
        pipeline_events=pipeline_events,
        inbox_banner=inbox_banner,
    )
```

- [ ] **Step 4: Create the widget partial** `job_finder/web/templates/dashboard/_degraded_sources.html` (clone the badge-pill style from `_dashboard_history.html:147-164`):

```jinja
{# Degraded parser sources (auto-heal Phase A). Required context: degraded (list[dict]). #}
<div class="bg-slate-800 rounded-xl border border-slate-700 p-4">
  <h3 class="text-sm font-semibold text-slate-300 mb-3">Parser Health</h3>
  {% if degraded %}
    <ul class="space-y-2">
      {% for d in degraded %}
        <li class="flex items-center justify-between text-sm">
          <span class="font-mono text-slate-200">{{ d.source }}</span>
          <span class="bg-red-900/40 text-red-400 border border-red-700/50 rounded px-2 py-0.5 text-xs">
            degraded · {{ d.consecutive_breaks }} misses
          </span>
        </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="text-slate-500 text-sm">All sources healthy.</p>
  {% endif %}
</div>
```

- [ ] **Step 5: Include the widget in `index.html`** inside a refetchable wrapper (mirror `dashboard/index.html:36-43`):

```jinja
<section aria-label="Parser health">
  <div id="degraded-sources"
       hx-get="{{ url_for('dashboard.degraded_sources_fragment') }}"
       hx-trigger="dashboard-refresh from:body, sse:jobs-changed"
       hx-swap="innerHTML">
    {% include "dashboard/_degraded_sources.html" %}
  </div>
</section>
```

- [ ] **Step 6: Run the dashboard tests**

Run: `uv run --active pytest tests/test_autoheal_dashboard.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/blueprints/dashboard.py job_finder/web/templates/dashboard/_degraded_sources.html job_finder/web/templates/dashboard/index.html tests/test_autoheal_dashboard.py
git commit -m "feat(autoheal): dashboard degraded-sources widget"
```

---

### Task 9: End-to-end integration test + full-suite gate

**Files:**
- Create: `tests/test_autoheal_integration.py`

- [ ] **Step 1: Write the integration test** — drives a source from healthy to degraded through `record_extraction` + `run_detection`, asserts the activity row and dashboard surface:

```python
# tests/test_autoheal_integration.py
import json
import sqlite3
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal import BREAK_THRESHOLD


def test_break_flips_degraded_and_logs_activity(app, client):
    db = app.config["DB_PATH"]
    with app.app_context():
        from job_finder.web.db_helpers import get_db
        conn = get_db(db)
        for _ in range(3):
            hm.record_extraction(conn, "indeed", "email", "x" * 400, job_count=4)
        for _ in range(BREAK_THRESHOLD):
            hm.record_extraction(conn, "indeed", "email", "x" * 400, job_count=0)
    flagged = hm.run_detection(db)
    assert "indeed" in flagged

    raw = sqlite3.connect(db)
    act = raw.execute(
        "SELECT metadata FROM user_activity WHERE action='source_degraded' AND entity_id='indeed'"
    ).fetchone()
    assert act is not None
    assert json.loads(act[0])["reason"] == "consecutive_zero_yields"

    assert "indeed" in client.get("/dashboard/").data.decode()
```

- [ ] **Step 2: Run it**

Run: `uv run --active pytest tests/test_autoheal_integration.py -q`
Expected: PASS.

- [ ] **Step 3: Full-suite regression gate (zero behavior change claim)**

Run: `uv run --active pytest -q --tb=short`
Expected: PASS — no pre-existing tests change behavior. Investigate any new failure before proceeding (do not defer).

- [ ] **Step 4: Commit**

```bash
git add tests/test_autoheal_integration.py
git commit -m "test(autoheal): end-to-end break→degraded→dashboard integration"
```

---

> **CHUNK 2 REVIEW GATE** — dispatch plan-document-reviewer over Chunk 2; fix until approved.

## Done criteria (Phase A)

- New ingestion runs capture a scrubbed corpus sample per extraction across all three surfaces (email full body; ATS/careers tiny baseline artifact).
- An **email** source that stops yielding jobs on meaningful inputs flips to `DEGRADED` after 3 consecutive misses, and never on genuine-empty/short inputs or sources with no positive history.
- ATS/careers record samples + baseline (`detect=False`) but do not auto-degrade in Phase A (their break detection lands in Phase B with raw-artifact capture).
- The dashboard shows degraded sources; healthy state shows "All sources healthy."
- A `source_degraded` activity row is written for each flip.
- Full pre-existing test suite still passes — no behavior change to parsing.

## Explicitly deferred to later phases (do NOT build now)

- `Strategy` chains / structured-data-first parsing (Phase B).
- Pre-filter raw ATS response capture & full careers HTML capture (Phase B refinement).
- Heal pipeline, code-gen, sandbox, shadow mode, upstream PR (Phases C–D).
- Per-company crawler health granularity (Phase A uses an aggregate `careers` row; per-company stays in `company_scan_log`).
