# Source Benchmark — Baseline (no-paid simulation) (2026-05-21)

- target_titles: Lead Product Analyst, Head of Analytics, Director of Analytics, Analytics Manager, Senior Manager, Analytics, Analytics Lead, Product Analytics Manager, Lead Analyst, Lead Data Analyst, Principal Analyst, Staff Data Scientist, Staff Product Data Scientist, Principal Data Scientist, Lead Data Scientist, Senior Data Scientist, Senior Product Data Scientist, Data Scientist, Data Science Manager, Marketing Data Scientist, Senior Marketing Data Scientist, Lead Marketing Analytics Data Scientist, Senior Analyst, Senior Data Analyst, Senior Business Analyst, Business Analyst, Healthcare Data Analyst, Experimentation Lead
- existing jobs in DB: 11330
- no-paid mode: yes

## Per-source counts

| source | raw | parse_ok | title_match | novel | overlap_pct | fetch_s | notes |
|--------|----:|---------:|------------:|------:|------------:|--------:|-------|
| gmail | 2267 | 2267 | 596 | 39 | 98.3% | 79.97 | - |
| portal_remoteok | 1 | 1 | 1 | 1 | 0.0% | 0.39 | - |
| portal_remotive | 0 | 0 | 0 | 0 | 0.0% | 0.32 | - |
| portal_himalayas | 20 | 20 | 0 | 20 | 0.0% | 26.19 | - |
| portal_jobicy | 1 | 1 | 0 | 1 | 0.0% | 0.53 | - |
| portal_yc_workatastartup | 30 | 30 | 0 | 30 | 0.0% | 24.95 | - |
| portal_usajobs | 0 | 0 | 0 | 0 | 0.0% | 0.0 | - |
| portal_adzuna | 0 | 0 | 0 | 0 | 0.0% | 0.0 | - |
| portal_jooble | 0 | 0 | 0 | 0 | 0.0% | 0.0 | - |

## Sample titles (top 5 per source)

### gmail
- Data Analytics Lead @ Solace
- Senior Staff, Advanced Analytics @ Airbnb
- Lead, Data Product Analyst @ ConcertoCare
- Analytics Engineer @ OpenAI
- Senior Data Scientist @ Virta Health

### portal_remoteok
- Business Analyst @ Judi Health

### portal_remotive
- (no results)

### portal_himalayas
- Senior Enterprise Relationship Manager @ BlackCloak
- Captive SME @ Crumdale Specialty
- Sales Development Representative @ Hey Lieu
- Operations Manager (LATAM, Remote) @ Adaptive Teams
- Technical Lead, Engineering @ Velsera

### portal_jobicy
- Machine Learning Engineer @ ManTech

### portal_yc_workatastartup
- Director of Software & Product @ SnapMagic
- Senior Software Developer @ Hive
- Senior Software Engineer, Data @ Hive
- Staff Software Engineer - Agent Core @ Tasklet
- Senior Software Engineer - Organizations & Teams @ Tasklet

### portal_usajobs
- (no results)

### portal_adzuna
- (no results)

### portal_jooble
- (no results)

## Comparison threshold (Q6)

After Stage 2-5 land, re-run this benchmark and check that the sum of free-source `novel` counts (gmail + imap + portal_*) is at least 80% of the sum of paid-source `novel` counts (serpapi + thordata + dataforseo) in this baseline.
 80% is a placeholder agreed during planning (Q6, user invited revision once baseline numbers landed) — revisit if the baseline makes it look unreachable or trivial.

## Stage 2 run commentary

Run config: `--no-paid` against `config.bench-stage2.yaml` (scratch copy of `config.yaml` with `portal_search.jobicy.enabled=true`, `portal_search.yc_workatastartup.enabled=true`, all other Stage 2 portals disabled because no credentials are registered). Scratch config was deleted after the run; real `config.yaml` is unchanged.

**Headline vs Stage 0 baseline:**

- Baseline paid-source novel: serpapi 0 + thordata 0 + dataforseo 9 = **9**
- Stage 2 free-portal novel (excluding gmail): remoteok 1 + remotive 0 + himalayas 20 + jobicy 1 + yc_workatastartup 30 + usajobs/adzuna/jooble 0 = **52**
- Acceptance threshold (80% of 9 = 7): **comfortably met by free-portal novel sum alone**.

**Acceptance criterion "non-zero `novel_count` for at least 3 of the 5 new sources":**

- Live yield: 2 of 5 (Jobicy 1, YC 30). The other 3 (USAJobs, Adzuna, Jooble) correctly short-circuited at the missing-credentials guard. Live numbers for the keyed-but-free portals require the user to register at the linked sites in `config.example.yaml` — until then, those three are structurally validated via unit tests only.
- Structural validation: 5 of 5 fetchers covered by `tests/test_portal_search_source.py` — basic success path, error path, and (for keyed portals) missing-credentials short-circuit.

**Discoveries:**

1. **Gmail now works in this install** — Stage 0 baseline showed `gmail: 0 (RuntimeError: Token file not found)`; this run shows gmail 2267 raw / 39 novel. `token.json` must have been created between Stage 0 and Stage 2. Future-session worth noting.
2. **Title-match weakness persists** for keyword-broad sources. YC returns 30 novel but 0 title-match because keywords from the user's wider `target_titles` (e.g., "Data Scientist") match the YC `query=` filter but the resulting titles ("Senior Software Engineer, Data") don't pass the strict word-boundary `_title_matches` regex. Same dynamic the Stage 0 STATUS.md flagged for Himalayas — Stage 2 inherits it rather than fixes it.
3. **YC dominates Stage 2 yield** (30/52 = 58% of free-portal novel). It's the highest-leverage of the keyless Stage 2 portals. Worth confirming the Inertia-data-page parse is still working in future stages — the YC fetcher is structurally fragile.
