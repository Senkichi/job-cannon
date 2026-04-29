# Eval Run — v4d2_per_axis_evidence

**Run ID:** `61d050bfcc7745f2addbd32ca32028c7`
**Variant:** v4d2_per_axis_evidence
**Baseline:** a8639457dbbe446aa1242023711f4a9d
**Timestamp:** 2026-04-29T00:39:49.688579+00:00

## Headline
- Apply false-positive rate: **0.182**
- vs baseline 0.182 → Δ +0.000 (EQUAL)
- Macro F1 (5-class): **0.316**
- Run-level health: 120 calls, 0 failed, schema adherence 100.0%

## Aggregated Metric Tables

### Per-Axis
| Axis | MAE | Bias | ICC(2,1) | QW-κ | n_used |
|---|---|---|---|---|---|
| title_fit | 0.850 | +0.450 | 0.669 | 0.663 | 40 |
| location_fit | 1.229 | -0.371 | 0.433 | 0.426 | 35 |
| comp_fit | 1.038 | -0.115 | 0.585 | 0.575 | 26 |
| domain_match | 1.062 | +0.688 | 0.481 | 0.473 | 32 |
| seniority_match | 0.974 | +0.821 | 0.518 | 0.511 | 39 |
| skills_match | 1.300 | +0.767 | 0.371 | 0.363 | 30 |

## Classification Metrics
| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| apply | 0.250 | 0.286 | 0.267 | 7 |
| consider | 0.000 | 0.000 | 0.000 | 4 |
| skip | 0.000 | 0.000 | 0.000 | 0 |
| reject | 0.750 | 0.462 | 0.571 | 13 |
| low_signal | 0.909 | 0.625 | 0.741 | 16 |

**Macro-F1:** 0.316

## Confusion Matrix
| (true \ pred) | apply | consider | skip | reject | low_signal |
|---|---|---|---|---|---|
| **apply** | 2 | 4 | 0 | 1 | 0 |
| **consider** | 3 | 0 | 0 | 1 | 0 |
| **skip** | 0 | 0 | 0 | 0 | 0 |
| **reject** | 3 | 3 | 0 | 6 | 1 |
| **low_signal** | 0 | 6 | 0 | 0 | 10 |

## Per-Job Diff
Jobs whose classification flipped or where any sub-score moved by ≥ 2 vs gold:

| dedup_key | gold_cls | pred_cls | flipped | sub-score deltas |
|---|---|---|---|---|
| `abbott laboratories|director system integration and analytics` | reject | reject |  | title_fit+2, location_fit-2, comp_fit-2, domain_match+3, seniority_match+4, skills_match+2 |
| `agilent|strategic analysis & planning lead` | consider | apply | YES | domain_match+2 |
| `axelon|(agile1) data engineer, senior` | low_signal | low_signal |  | location_fit-2 |
| `chevron|fuels chemist` | reject | reject |  | seniority_match+2 |
| `chime|lead data analyst` | apply | consider | YES | title_fit-3, comp_fit-3, domain_match-2, skills_match-3 |
| `cologix|senior manager, corporate development (remote: usa)` | reject | consider | YES | location_fit+4 |
| `fora financial|vp of data and ai` | reject | apply | YES | title_fit+3, location_fit+4, seniority_match+4, skills_match+2 |
| `gallup|senior data scientist – product analytics & strategy` | low_signal | low_signal |  | skills_match+2 |
| `general electric|senior services manager – analytics & tools leader` | reject | apply | YES | title_fit+2, location_fit+3, comp_fit+2, seniority_match+2, skills_match+3 |
| `google deepmind|research engineer, frontier safety mitigations, deepmind` | low_signal | consider | YES | skills_match+2 |
| `harbin clinic|epic application analyst-cadence` | low_signal | consider | YES | domain_match+2 |
| `highlevel|senior director - data science & analytics - remote` | reject | apply | YES | title_fit+3, location_fit+2, comp_fit+2, domain_match+2, seniority_match+4, skills_match+3 |
| `infosys|principal sap fico consultant` | reject | low_signal | YES | skills_match+2 |
| `jobleads-us|analytics manager` | low_signal | consider | YES | — |
| `jupiter medical center|lead epic analyst - inpatient` | low_signal | low_signal |  | location_fit+2, domain_match+2 |
| `latent (ca)|machine learning engineer` | low_signal | consider | YES | skills_match+3 |
| `meta|senior analyst (competitive intelligence)` | apply | reject | YES | location_fit-3, skills_match-2 |
| `playstation|business analyst, supply chain (contract)` | apply | consider | YES | title_fit-2, location_fit-2, comp_fit-3 |
| `robinhood|staff data scientist, product (crypto)` | consider | apply | YES | — |
| `roblox|[2026] data scientist - phd intern (short term)` | reject | reject |  | location_fit-3 |
| `roblox|senior data scientist - growth measurement` | apply | consider | YES | location_fit-2 |
| `sarnova|technical project manager – post implementation success - digitech - remote` | low_signal | consider | YES | comp_fit+2, domain_match+2, seniority_match+2, skills_match+2 |
| `spyre therapeutics|senior director, biostatistics` | reject | consider | YES | — |
| `state of california state personnel board|research data analyst` | reject | reject |  | location_fit-2 |
| `state of wisconsin investment board|data analytics engineering manager` | low_signal | low_signal |  | title_fit+2, location_fit+2, domain_match+2 |
| `tekwissen|data analyst` | low_signal | low_signal |  | location_fit-3 |
| `thales|enablement analytics & reporting manager` | consider | reject | YES | location_fit-4 |
| `the realreal|analytics manager, supply sales analytics` | apply | consider | YES | comp_fit-3 |
| `upward health|revenue cycle product manager` | reject | reject |  | skills_match+2 |
| `vera therapeutics|tmf manager, clinical qa` | reject | consider | YES | domain_match+2 |
| `veritas management|project manager- evaluation and research` | low_signal | consider | YES | comp_fit+2, skills_match+2 |
| `verse|senior data scientist` | consider | apply | YES | comp_fit+2 |

## Cost / Latency
- Total scoring calls: 120
- Failed calls: 0
- Schema adherence: 100.0%

## Coherence Violations
Rate: 15.0% (6 of 40 jobs)

- axis=domain_match score=4 gaps_text="jd silent on exact compensation domain slightly different from healthcare/saas"
- axis=skills_match score=4 gaps_text="industry mismatch partial technical skill overlap"
- axis=skills_match score=5 gaps_text="compensation not listed jd silent on some technical skills"
- axis=location_fit score=5 gaps_text="compensation below floor on-site requirement"
- axis=location_fit score=4 gaps_text="domain mismatch (energy vs healthcare) hybrid location preference"
- axis=domain_match score=4 gaps_text="domain slightly different from healthcare and saas"
