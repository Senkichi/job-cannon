# NEXT_STEPS — ATS Coverage Audit (recovered + refreshed)

> **Recovered re-baseline for issue #452.** The original `NEXT_STEPS_ATS_COVERAGE.md`
> lived in the gitignored `.planning/` tree and was lost (no copy in git history,
> repo root, or `.planning/`). This file replaces it and is **git-tracked** (via a
> single-file `!.planning/NEXT_STEPS_ATS_COVERAGE.md` negation in `.gitignore`) so it
> cannot be lost again. Regenerate it from `scripts/audit_ats_coverage.py`.

## How to refresh

```bash
uv run python scripts/audit_ats_coverage.py        # read-only; reads $JC_DB_PATH or the canonical jobs.db
```

The script is strictly read-only (`mode=ro` connection, `SELECT`/`GROUP BY` only).
The "supported platform" set is derived **at runtime** from the live scanner
registry (`job_finder.web.ats_platforms.SCANNERS_BY_NAME`) — not a hardcoded list —
so it tracks new scanners automatically. The "custom" bucket follows the m074
definition: `ats_probe_status='miss' AND (ats_platform IS NULL OR ats_platform='')`.

## Refreshed distribution (run 2026-06-18, live DB, 4 250 companies)

Supported scanners (16, from registry): ashby, bamboohr, breezy, greenhouse,
jazzhr, jobvite, lever, paylocity, personio, pinpoint, recruitee, rippling,
smartrecruiters, teamtailor, workable, workday.

Full `ats_platform` distribution:

| ats_platform      | count | scanner? |
|-------------------|------:|----------|
| (null)            | 3 205 | —        |
| greenhouse        |   488 | ✅       |
| ashby             |   233 | ✅       |
| workday           |   146 | ✅       |
| lever             |    92 | ✅       |
| smartrecruiters   |    43 | ✅       |
| pinpoint          |    35 | ✅       |
| jobvite           |     3 | ✅ (no public API) |
| bamboohr          |     1 | ✅       |
| breezy            |     1 | ✅       |
| recruitee         |     1 | ✅       |
| rippling          |     1 | ✅       |
| workable          |     1 | ✅       |

Probe-status × has-platform: `hit` 1 039 with platform / 3 no platform;
`miss` 6 with platform / 3 181 no platform; `pending` 10; `error` 11.

## Uncrawlable cohort (refreshed)

| cohort                          | count |
|---------------------------------|------:|
| **uncrawlable total**           | 3 181 |
| custom (miss + no platform, m074) | 3 181 |
| of which already `scan_enabled=0` | 2 589 |
| unsupported stored platform     |     0 |

## Named cohorts (Phenom / iCIMS / UKG / custom) — KEY FINDING

| cohort  | Apr 14 (stale) | 2026-06-18 (refreshed) | feasibility note |
|---------|---------------:|-----------------------:|------------------|
| Phenom  | 24             | **0** | No rows carry `ats_platform='phenom'`. Phenom career sites expose a per-tenant JSON/GraphQL API but it's tenant-obfuscated — feasible only after the sub-cohort is re-derived. |
| iCIMS   | 22             | **0** | No rows carry `ats_platform='icims'`. iCIMS has a public job-search endpoint but is usually iframe-embedded — partial feasibility. |
| UKG     | 20             | **0** | No rows carry `ats_platform='ukg'`. UKG Pro Recruiting exposes `/JobBoard/...` JSON for some tenants — feasible per-tenant. |
| custom  | 282            | **3 181** | Bulk of the cohort. No public API by definition; not a scanner target — needs per-site careers-crawler work, not a registry scanner. |

**The Apr 14 named-platform counts have collapsed to zero.** The current DB does
not store `phenom` / `icims` / `ukg` as `ats_platform` values at all — the entire
non-supported cohort is the null/custom (`miss` + no platform) bucket. The earlier
"Phenom 24 / iCIMS 22 / UKG 20" sizing was evidently derived from a different
signal (e.g. homepage-URL pattern matching during a probe pass), not from a stored
`ats_platform` label, and that signal is no longer materialized in the table.

## Ranked next steps (re-derived)

1. **Do NOT build Phenom / iCIMS / UKG scanners on the stale counts.** Their
   stored-label cohorts are empty today. Before any scanner work, **re-derive the
   sub-cohorts from a live signal** — probe the 3 205 null-platform companies'
   homepage URLs for `phenompeople.com` / `icims.com` / `ukg`/`ultipro` patterns and
   record the result (a new probe pass, separate downstream issue). Scanner ROI must
   be sized against *that* refreshed count, not Apr 14's.
2. **Custom cohort (3 181, 2 589 already disabled)** is not a scanner target —
   it's careers-crawler / agentic-enricher territory. No registry scanner applies.
3. **Supported-platform coverage is healthy** — every company with a detected
   `ats_platform` maps to a live scanner; there is no scanner gap among
   *classified* companies. The opportunity is in *classifying* the 3 205 null rows,
   not in adding scanners for unsupported platforms (there are none).

_Generated for #452 (parent epic #449). Re-run the script above to refresh._
