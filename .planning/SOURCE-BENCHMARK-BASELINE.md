# Source Benchmark — Baseline (2026-05-21)

- target_titles: Lead Product Analyst, Head of Analytics, Director of Analytics, Analytics Manager, Senior Manager, Analytics, Analytics Lead, Product Analytics Manager, Lead Analyst, Lead Data Analyst, Principal Analyst, Staff Data Scientist, Staff Product Data Scientist, Principal Data Scientist, Lead Data Scientist, Senior Data Scientist, Senior Product Data Scientist, Data Scientist, Data Science Manager, Marketing Data Scientist, Senior Marketing Data Scientist, Lead Marketing Analytics Data Scientist, Senior Analyst, Senior Data Analyst, Senior Business Analyst, Business Analyst, Healthcare Data Analyst, Experimentation Lead
- existing jobs in DB: 11330
- no-paid mode: no

## Per-source counts

| source | raw | parse_ok | title_match | novel | overlap_pct | fetch_s | notes |
|--------|----:|---------:|------------:|------:|------------:|--------:|-------|
| gmail | 0 | 0 | 0 | 0 | 0.0% | 0.22 | RuntimeError: Token file not found: token.json. Run: python -m job_finder.gmail_auth |
| serpapi | 150 | 150 | 120 | 0 | 100.0% | 11.94 | - |
| thordata | 0 | 0 | 0 | 0 | 0.0% | 2.29 | - |
| dataforseo | 634 | 634 | 420 | 9 | 98.6% | 83.93 | - |
| portal_remoteok | 1 | 1 | 1 | 1 | 0.0% | 0.6 | - |
| portal_remotive | 0 | 0 | 0 | 0 | 0.0% | 0.3 | - |
| portal_himalayas | 20 | 20 | 0 | 20 | 0.0% | 27.0 | - |

## Sample titles (top 5 per source)

### gmail
- (no results)

### serpapi
- Staff Data Scientist, Research, Search Health @ Google
- Staff Data Scientist, Product Analytics - Creatives @ Moloco
- Staff Data Scientist - Core Products @ Gusto
- Staff Data Scientist, Algorithm - Financial Markets @ Airwallex
- Staff Data Scientist (Pricing) @ GoFundMe

### thordata
- (no results)

### dataforseo
- Manager, One Client and Sales Analytics @ BMO Capital Markets
- Analytics Manager, Supply Sales Analytics @ The RealReal
- Director, Business Intelligence & Analytics Engineering @ Qcells
- Analytics Manager, Supply Sales Analytics @ The RealReal
- Manager, Audit and Analytics @ Gilead Sciences

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

## Comparison threshold (Q6)

After Stage 2-5 land, re-run this benchmark and check that the sum of free-source `novel` counts (gmail + imap + portal_*) is at least 80% of the sum of paid-source `novel` counts (serpapi + thordata + dataforseo) in this baseline.
 80% is a placeholder agreed during planning (Q6, user invited revision once baseline numbers landed) — revisit if the baseline makes it look unreachable or trivial.
