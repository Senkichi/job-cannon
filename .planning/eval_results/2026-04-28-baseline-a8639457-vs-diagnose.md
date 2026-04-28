# Eval Run — baseline

**Run ID:** `a8639457dbbe446aa1242023711f4a9d`
**Variant:** baseline
**Baseline:** (none — diagnose mode)
**Timestamp:** 2026-04-28T20:30:07.312090+00:00

## Headline
- Apply false-positive rate: **0.182**
- Macro F1 (5-class): **0.373**
- Run-level health: 120 calls, 0 failed, schema adherence 100.0%

## Aggregated Metric Tables

### Per-Axis
| Axis | MAE | Bias | ICC(2,1) | QW-κ | n_used |
|---|---|---|---|---|---|
| title_fit | 1.075 | +0.275 | 0.452 | 0.446 | 40 |
| location_fit | 1.371 | -0.571 | 0.342 | 0.335 | 35 |
| comp_fit | 1.115 | -0.038 | 0.462 | 0.453 | 26 |
| domain_match | 0.938 | +0.375 | 0.458 | 0.450 | 32 |
| seniority_match | 1.128 | +0.359 | 0.355 | 0.349 | 39 |
| skills_match | 1.167 | +0.167 | 0.437 | 0.429 | 30 |

## Classification Metrics
| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| apply | 0.333 | 0.429 | 0.375 | 7 |
| consider | 0.100 | 0.250 | 0.143 | 4 |
| skip | 0.000 | 0.000 | 0.000 | 0 |
| reject | 0.700 | 0.538 | 0.609 | 13 |
| low_signal | 0.909 | 0.625 | 0.741 | 16 |

**Macro-F1:** 0.373

## Confusion Matrix
| (true \ pred) | apply | consider | skip | reject | low_signal |
|---|---|---|---|---|---|
| **apply** | 3 | 3 | 0 | 1 | 0 |
| **consider** | 2 | 1 | 0 | 1 | 0 |
| **skip** | 0 | 0 | 0 | 0 | 0 |
| **reject** | 3 | 2 | 0 | 7 | 1 |
| **low_signal** | 1 | 4 | 0 | 1 | 10 |

## Per-Job Diff
Jobs whose classification flipped or where any sub-score moved by ≥ 2 vs gold:

| dedup_key | gold_cls | pred_cls | flipped | sub-score deltas |
|---|---|---|---|---|
| `abbott laboratories|director system integration and analytics` | reject | reject |  | location_fit-2, comp_fit-2, domain_match+2, seniority_match+2, skills_match+2 |
| `agilent|strategic analysis & planning lead` | consider | apply | YES | — |
| `chevron|fuels chemist` | reject | reject |  | location_fit+2, seniority_match+2 |
| `chime|lead data analyst` | apply | consider | YES | title_fit-3, location_fit-2, comp_fit-3, domain_match-2, seniority_match-2, skills_match-3 |
| `cologix|senior manager, corporate development (remote: usa)` | reject | apply | YES | location_fit+4, domain_match+2 |
| `evernorth sales operations|business analytics senior analyst` | low_signal | low_signal |  | title_fit-2 |
| `fora financial|vp of data and ai` | reject | apply | YES | location_fit+4, seniority_match+2, skills_match+2 |
| `frontdoor|lead people technology analyst - total rewards` | low_signal | low_signal |  | domain_match-2 |
| `gallup|senior data scientist – product analytics & strategy` | low_signal | low_signal |  | location_fit-4, skills_match+2 |
| `general electric|senior services manager – analytics & tools leader` | reject | consider | YES | domain_match+2, skills_match+2 |
| `google deepmind|research engineer, frontier safety mitigations, deepmind` | low_signal | consider | YES | title_fit+2, location_fit-2 |
| `harbin clinic|epic application analyst-cadence` | low_signal | consider | YES | title_fit+2, seniority_match+2 |
| `highlevel|senior director - data science & analytics - remote` | reject | apply | YES | title_fit+3, location_fit+2, comp_fit+2, seniority_match+4, skills_match+2 |
| `infosys|principal sap fico consultant` | reject | low_signal | YES | — |
| `jazz pharmaceuticals|associate director, medical safety (scientist)` | low_signal | low_signal |  | title_fit+2, seniority_match+2 |
| `jobleads-us|analytics manager` | low_signal | consider | YES | title_fit-2, seniority_match-2 |
| `latent (ca)|machine learning engineer` | low_signal | apply | YES | title_fit+4, seniority_match+2, skills_match+2 |
| `meta|senior analyst (competitive intelligence)` | apply | reject | YES | location_fit-3, skills_match-2 |
| `nerdwallet|lead data analyst` | apply | consider | YES | title_fit-2, seniority_match-3 |
| `playstation|business analyst, supply chain (contract)` | apply | apply |  | comp_fit-2, seniority_match-2, skills_match-2 |
| `robinhood|staff data scientist, product (crypto)` | consider | consider |  | location_fit-2 |
| `roblox|[2026] data scientist - phd intern (short term)` | reject | reject |  | title_fit+2, location_fit-2 |
| `sarnova|technical project manager – post implementation success - digitech - remote` | low_signal | consider | YES | comp_fit+2, domain_match+2, seniority_match+2, skills_match+2 |
| `spyre therapeutics|senior director, biostatistics` | reject | reject |  | domain_match-2 |
| `state of wisconsin investment board|data analytics engineering manager` | low_signal | low_signal |  | location_fit+2, domain_match+2 |
| `tekwissen|data analyst` | low_signal | low_signal |  | location_fit-3 |
| `thales|enablement analytics & reporting manager` | consider | reject | YES | location_fit-4 |
| `the realreal|analytics manager, supply sales analytics` | apply | consider | YES | location_fit-2, comp_fit-3 |
| `upward health|revenue cycle product manager` | reject | reject |  | skills_match+2 |
| `vera therapeutics|tmf manager, clinical qa` | reject | consider | YES | comp_fit+2 |
| `vericast|data analyst iv - marketing analytics & digital insights` | reject | reject |  | comp_fit+3 |
| `veritas management|project manager- evaluation and research` | low_signal | reject | YES | comp_fit+2 |
| `verse|senior data scientist` | consider | apply | YES | comp_fit+2 |
| `zip|senior/staff data scientist, epd` | apply | apply |  | location_fit-2 |

## Cost / Latency
- Total scoring calls: 120
- Failed calls: 0
- Schema adherence: 100.0%

## Coherence Violations
Rate: 20.0% (8 of 40 jobs)

- axis=location_fit score=4 gaps_text="title mismatch (data engineer vs. analytics roles) location constraint (oakland, ca) compensation not listed domain mism"
- axis=domain_match score=4 gaps_text="domain is saas/b2b2c, candidate has healthcare background some skills overlap but not direct match for all listed tools"
- axis=seniority_match score=4 gaps_text="domain is tech, not healthcare or e-commerce seniority may be slightly off if candidate is more senior"
- axis=location_fit score=4 gaps_text="title mismatch (business analyst vs. lead product analyst, etc.) location is san mateo, ca which may not be a target loc"
- axis=domain_match score=4 gaps_text="hybrid work schedule may be a partial commute for remote candidates domain is gaming, which might be different from heal"
- axis=location_fit score=4 gaps_text="location is san francisco, which may require relocation domain is energy markets, which is adjacent but not direct match"
- axis=domain_match score=4 gaps_text="compensation not listed, assume market rate domain slightly different (real estate vs. healthcare)"
- axis=comp_fit score=4 gaps_text="candidate prefers remote work, role is on-site salary range slightly below candidate's floor"
