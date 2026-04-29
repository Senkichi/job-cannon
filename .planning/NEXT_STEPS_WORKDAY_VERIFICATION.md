# Workday Fix Verification — Implementation Plan

**Created:** 2026-04-29
**Owner:** new session (handed off from prior session)
**Estimated wall time:** 8–10h (mostly waiting on Ollama scoring)
**Estimated cost:** ~$1 in Sonnet judge calls

---

## Why this exists

The 04-28 → 04-29 wholesale rescore produced output where 87% of Workday-sourced jobs (106/122) had `jd_full = "Workday"` literally — a 6-character string, not a real description. Root cause was a 2-bug cascade in `job_finder/web/ats_platforms.py` (URL template) and `job_finder/web/enrichment_tiers.py` (no JD-quality validation). Both fixed in commit `30ff5e0`. Migration 46 healed the data: source URLs repaired, the 106 corrupt `jd_full` values nullified, classifications reset.

**This plan verifies the heal landed cleanly** by forcing the enrichment + scoring cycle through the corrected pipeline and re-running the rescore-quality audit to confirm Workday classifications are now real, not noise.

The baseline audit (pre-fix) is at `.planning/eval-reports/rescore-audit-2026-04-29T191628Z.md`. Compare your post-heal audit against it.

---

## Read first (5 min)

| Doc | Why |
|---|---|
| `.planning/eval-reports/rescore-audit-2026-04-29T191628Z.md` | Baseline metrics + the Workday discovery narrative |
| `git log -1 30ff5e0` | The fix commit message (full root-cause writeup) |
| `git log -1 386b5e9` | The audit harness commit |
| `scripts/audit_rescore_quality.py` | The audit harness you'll re-run in Phase 3 |

Memory worth checking: `feedback_bulk_rescore_pause_schedulers.md` (auto-loaded).

---

## Pre-flight checks

Before starting, verify the heal is in place:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
checks = {
    'DB user_version (must be 46)': conn.execute('PRAGMA user_version').fetchone()[0],
    'Source URLs with /job//job/ (must be 0)': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE source_urls LIKE '%/job//job/%'\").fetchone()[0],
    'jd_full == Workday (must be 0)': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE TRIM(jd_full) = 'Workday'\").fetchone()[0],
    'Workday rows total': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE sources LIKE '%Workday%'\").fetchone()[0],
    'Workday rows with NULL jd_full (await enrichment)': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE sources LIKE '%Workday%' AND jd_full IS NULL\").fetchone()[0],
    'Total unclassified': conn.execute('SELECT COUNT(*) FROM jobs WHERE classification IS NULL').fetchone()[0],
    '  ...with jd_full ready to score': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE classification IS NULL AND jd_full IS NOT NULL AND TRIM(jd_full) != ''\").fetchone()[0],
    '  ...awaiting enrichment': conn.execute(\"SELECT COUNT(*) FROM jobs WHERE classification IS NULL AND (jd_full IS NULL OR TRIM(jd_full) = '')\").fetchone()[0],
}
for k, v in checks.items():
    print(f'  {k}: {v}')
"
```

Stop and investigate if `user_version != 46` or any of the "must be 0" checks return nonzero. Migration didn't run — Flask may need restart.

Also confirm Flask is running: `curl.exe -s http://localhost:5000/admin/jobs | head` — should return JSON with the scheduled jobs list.

---

## Phase 1 — Drain the enrichment backlog

**Goal:** every unclassified job ends up with a real `jd_full` so scoring can act on it. Specifically, the ~108 Workday rows reset by Migration 46 must re-fetch via the corrected `/wday/cxs/.../job/...` API endpoint.

### Step 1.1 — Pause schedulers that race the enrichment loop

You're about to drive enrichment + scoring manually. Pause the cron jobs that would otherwise pull from the same `classification IS NULL` pool:

```powershell
curl.exe -X POST http://localhost:5000/admin/jobs/enrichment_backfill/pause
curl.exe -X POST http://localhost:5000/admin/jobs/agentic_backfill/pause
```

Verify both show `paused: True`:
```powershell
curl.exe -s http://localhost:5000/admin/jobs | python -c "import json,sys; [print(j['id'], j['paused']) for j in json.load(sys.stdin)['jobs'] if j['id'] in ('enrichment_backfill','agentic_backfill')]"
```

### Step 1.2 — Write a one-shot enrichment driver

The existing `enrichment_backfill` cron uses `data_enricher.run_enrichment_backfill(db_path, serpapi_key, config, limit=200)`. Write a CLI driver that loops it until exhaustion. Place at `scripts/run_enrichment_drain.py`. Mirror the structure of `scripts/run_wholesale_rescore.py` (logging, --log flag, --limit, --progress-every).

Required behavior:
- Load `config.yaml`
- Loop: call `run_enrichment_backfill(db_path, serpapi_key=cfg.sources.serpapi.api_key, config=cfg, limit=500)`
- Print enriched count after each batch
- Stop when a batch returns 0 (or when total exceeds an upper-bound safety cap, e.g. 5000)
- Log to `backups/post-workday-fix-{timestamp}/enrichment.log`
- Final tally: total enriched + remaining `jd_full IS NULL` count

Smoke-test the driver with `--limit 25 --once` on a fresh shell before letting it run free.

### Step 1.3 — Run the drain

Expect the run to take 1–3h depending on free-tier hit rates. Monitor by streaming the log file or polling:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
n = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE classification IS NULL AND (jd_full IS NULL OR TRIM(jd_full) = '')\").fetchone()[0]
print(f'remaining awaiting enrichment: {n}')
conn.close()
"
```

Done when the count reaches the same floor you'd expect from genuinely-unenrichable jobs (mark them `enrichment_tier = 'exhausted'` and move on — the cascade will tag them).

### Step 1.4 — Spot-check Workday recovery

Before moving to scoring, verify the Workday rows now have real JDs:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
rows = conn.execute(\"\"\"
    SELECT dedup_key, LENGTH(jd_full) AS n, SUBSTR(jd_full, 1, 80) AS preview
      FROM jobs WHERE sources LIKE '%Workday%' AND jd_full IS NOT NULL
     ORDER BY n DESC LIMIT 5
\"\"\").fetchall()
for r in rows: print(r)
print('---')
n_short = conn.execute(\"\"\"
    SELECT COUNT(*) FROM jobs WHERE sources LIKE '%Workday%' AND jd_full IS NOT NULL AND LENGTH(jd_full) < 200
\"\"\").fetchone()[0]
print(f'Workday rows with jd_full < 200 chars: {n_short}  (must be 0)')
conn.close()
"
```

If `n_short > 0`, the SPA-shell guard isn't catching something. Inspect those rows manually before continuing.

---

## Phase 2 — Score the enriched pool

**Goal:** every job with a real `jd_full` gets a real classification.

### Step 2.1 — Confirm the scoring backlog matches expectations

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
n = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE classification IS NULL AND jd_full IS NOT NULL AND TRIM(jd_full) != '' AND pipeline_status NOT IN ('dismissed', 'archived')\").fetchone()[0]
print(f'jobs ready to score: {n}')
"
```

Approximate expected ranges:
- ~1,800–2,000 if Phase 1 enrichment was thorough
- Includes the ~108 healed Workday rows + ~1,700 non-Workday rows that accumulated during the original 13h rescore window

### Step 2.2 — Run the scoring driver

Reuse `scripts/run_wholesale_rescore.py` — it already does what you need. Detached background run:

```powershell
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$dir = "backups\post-workday-fix-$ts"
New-Item -ItemType Directory -Force -Path $dir
Start-Process -NoNewWindow -RedirectStandardOutput "$dir\rescore.log" -RedirectStandardError "$dir\rescore.err" `
    uv -ArgumentList "run","python","scripts/run_wholesale_rescore.py","--log","$dir\rescore.log","--progress-every","25"
```

Expected runtime: ~7–8h on Ollama qwen2.5:14b at ~316/h. The CLI driver writes `DONE: total=N scored=N skipped=N excluded=N errored=N` when complete.

### Step 2.3 — Resume schedulers

Once scoring is done:

```powershell
curl.exe -X POST http://localhost:5000/admin/jobs/enrichment_backfill/resume
curl.exe -X POST http://localhost:5000/admin/jobs/agentic_backfill/resume
```

Verify both show `paused: False` with valid `next_run_time`.

---

## Phase 3 — Re-audit and compare

**Goal:** confirm the post-heal classifications are higher quality than the pre-heal baseline.

### Step 3.1 — Run the audit harness

```bash
uv run python scripts/audit_rescore_quality.py --sample-per-class 15 -v
```

Expected runtime: ~30 min (60 Sonnet calls × ~30s each on the Claude CLI subprocess path). Output will be at `.planning/eval-reports/rescore-audit-{timestamp}.md`.

### Step 3.2 — Compare against baseline

The two reports to diff:
- **Baseline (pre-fix):** `.planning/eval-reports/rescore-audit-2026-04-29T191628Z.md`
- **Post-heal:** the new report from Step 3.1

Pull the headline numbers from both, side by side:

| Metric | Baseline | Post-heal | Direction |
|---|---|---|---|
| Track 1 gold exact agreement | 55.9% | ? | up = good |
| Track 2 judge defensibility (exact + adjacent) | 73.3% | ? | up = good |
| Apply class — gold recall | 0% (0/6) | ? | up = good |
| Apply class — gold precision | 0% (0/4) | ? | up = good |
| Reject class — judge exact agreement | 14/15 | ? | flat = good |
| Top concern: `domain_mismatch` | 15 | ? | down = good |
| Top concern: `boilerplate_output` | 6 | ? | down = good (this was a Workday symptom) |

### Step 3.3 — Spot-check Workday classifications specifically

The bug primarily affected Workday rows. Check that those now classify reasonably:

```bash
uv run python -c "
import sqlite3, json
conn = sqlite3.connect('jobs.db')
from collections import Counter
rows = conn.execute(\"\"\"
    SELECT classification, sub_scores_json
      FROM jobs
     WHERE sources LIKE '%Workday%'
       AND classification IS NOT NULL
\"\"\").fetchall()
cls_dist = Counter(r[0] for r in rows)
print('Workday classification distribution (post-heal):')
for c, n in cls_dist.most_common(): print(f'  {c}: {n}')

# All sub-scores should be valid (1-5), not all 1s/2s anymore
all_low = sum(1 for c, s in rows if s and all(v <= 2 for v in json.loads(s).values()))
print(f'Workday rows with all sub-scores <= 2 (was the SPA-shell signature): {all_low}/{len(rows)}')
"
```

Pre-heal, ~106 Workday rows had all sub-scores ≤ 2 because the JD was empty. Post-heal, that number should drop dramatically (only legitimately-bad-fit Workday jobs should remain in that bucket).

---

## Success criteria

The fix is verified clean when ALL of these hold:

1. **No SPA-shell artifacts:** `SELECT COUNT(*) FROM jobs WHERE TRIM(jd_full) = 'Workday'` returns 0
2. **Workday rows have real JDs:** median `LENGTH(jd_full)` for Workday rows > 1000 chars
3. **Track 1 gold agreement increases** (or stays the same — the gold set has only 0–2 Workday rows, so impact may be small)
4. **Apply recall improves** even if marginally — the 6 gold-applies should be re-examined, with at least 2 of them now correctly classified or with defensible reasoning
5. **`boilerplate_output` concern count drops** in Track 2 by ≥ 30%
6. **No regression in reject precision** — should remain ≥ 67% (Track 1) and ≥ 90% (Track 2)

If criteria 1–2 fail: the URL fix or SPA guard isn't working. Re-investigate `ats_platforms.py:383` and `enrichment_tiers.py`'s `_MIN_VALID_JD_CHARS` gate.

If criteria 3–6 fail: the underlying scoring weakness (strict all-≥-3 apply rule, conservative axis grading) is the next thing to address. Don't conflate that with the Workday fix — that's separate, deferred work.

---

## Rollback (only if something goes catastrophically wrong)

You shouldn't need this, but for completeness:

- The fix is in code (commit `30ff5e0`). To revert: `git revert 30ff5e0` and restart Flask.
- Migration 46's data changes are mostly forward-compatible — the only destructive part is the `classification = NULL` reset, which the next batch scoring run will repopulate. There's no need to roll back Migration 46.
- Worst case: re-clone from `backups/pre-wholesale-rescore-20260428-220230/` (the pre-rescore backup of `jobs.db`) — but that loses all enrichment + scoring done since 04-28.

---

## Deliverables for the next session

When you finish:
1. Drop the new audit report path into the conversation summary
2. Note any success-criteria failures
3. Update memory with any surprising findings (use the auto-memory system)
4. If everything passes, consider closing this plan with a one-line note in `.planning/STATE.md` (e.g., "Workday URL bug verified clean YYYY-MM-DD")
