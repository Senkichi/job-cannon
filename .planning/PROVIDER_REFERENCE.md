# Provider Reference — LLM Model Evaluation & Rate Limits

> **Last verified**: 2026-03-29
> **Purpose**: Exhaustive reference for all LLM API providers evaluated for Job Cannon's scoring pipeline
> **Use case**: Replace Sonnet-tier deep evaluation (~2,500 input + ~220 output tokens per job)

---

## Table of Contents

1. [Measurement Methodology](#measurement-methodology)
2. [Model Quality Leaderboard](#model-quality-leaderboard)
3. [Provider: SambaNova](#provider-sambanova)
4. [Provider: OpenRouter](#provider-openrouter)
5. [Provider: Gemini (Google)](#provider-gemini-google)
6. [Provider: Ollama (Local)](#provider-ollama-local)
7. [Provider: Anthropic (Current)](#provider-anthropic-current)
8. [Provider: Cohere](#provider-cohere)
9. [Provider: Mistral](#provider-mistral)
10. [Per-Job Cost Comparison](#per-job-cost-comparison)
11. [Rate Limit Summary](#rate-limit-summary)
12. [Local Hardware Profile](#local-hardware-profile)
13. [New Provider Research](#new-provider-research)
14. [Recommendations](#recommendations)

---

## Measurement Methodology

### Baseline
- **Gold standard**: Claude Opus 4.6 scores via `claude -p` CLI (Max subscription, no API cost)
- **61 opus-scored jobs** in the database, stratified across score buckets (0-19, 20-39, 40-59, 60-79, 80-100)
- **Primary metric**: Pearson correlation (r) between candidate model scores and Opus baseline
- **Secondary metrics**: Schema adherence rate, median latency, P95 latency

### Prompt Configuration
- **Best prompt variant**: `fewshot` — adds 5 calibration examples (score 15, 38, 62, 78, 91) to the system prompt
- **Fewshot consistently outperforms** default, rubric, and fewshot-rubric variants across all models tested

### Token Usage (Measured)
- **Input tokens per job**: 1,441 (min) / 2,724 (median) / 3,430 (max)
- **Output tokens per job**: 208 (min) / 226 (median) / 236 (max)
- **Average**: 2,532 input + 223 output = **2,755 total tokens/job**
- JD character lengths: 4 (min) / 7,022 (median) / 8,000 (max, truncated)

---

## Model Quality Leaderboard

Best run per model, ranked by Pearson r with Opus baseline. All runs use fewshot prompt variant unless noted.

| # | Provider/Model | r | Schema | n | Med. Lat | P95 Lat | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **SN/Meta-Llama-3.3-70B-Instruct** | **0.935** | 100% | 18/18 | 1.9s | 3.3s | SUITABLE |
| 2 | **SN/DeepSeek-V3.2** | **0.934** | 95% | 18/19 | 5.0s | 69.1s | MARGINAL |
| 3 | **SN/Qwen3-235B** | **0.905** | 100% | 19/19 | 4.5s | 26.5s | SUITABLE |
| 4 | SN/DeepSeek-V3-0324 | 0.892 | 73% | 37/51 | 2.6s | 5.3s | NOT_REC |
| 5 | SN/DeepSeek-V3.1 | 0.878 | 100% | 19/19 | 2.7s | 4.4s | SUITABLE |
| 6 | **Ollama/qwen2.5:14b** | **0.852** | 94% | 48/51 | 16.4s | 18.6s | MARGINAL |
| 7 | Ollama/qwen2.5:32b | 0.817 | 92% | 47/51 | 116.0s | 143.1s | MARGINAL |
| 8 | SN/Llama-4-Maverick-17B-128E | 0.724 | 100% | 19/19 | 1.7s | 2.8s | MARGINAL |
| 9 | Ollama/gemma3:27b | 0.681 | 86% | 44/51 | 106.3s | 121.6s | NOT_REC |
| 10 | Ollama/deepseek-r1:14b | 0.647 | 90% | 46/51 | 25.2s | 51.8s | NOT_REC |
| 11 | Cohere/command-a-03-2025 | 0.613 | 100% | 20/20 | 16.1s | 53.4s | NOT_REC |
| 12 | Mistral/mistral-small | 0.538 | 100% | 20/20 | 5.8s | 11.1s | NOT_REC |
| 13 | Ollama/qwen3:14b | 0.470 | 75% | 15/20 | 23.9s | 51.3s | NOT_REC |

**Notes**:
- SambaNova n=18-19 results are from small samples (one day's 20 RPD quota) — high correlations may partially reflect sampling variance
- Ollama n=47-48 results are more statistically robust (larger sample from full 51-job Opus set)
- All alternative models exhibit **systematic score inflation** on poor-fit jobs (Opus 8 -> model 42+) but preserve rank ordering
- OpenRouter free models: 0% schema adherence across all tested models (Llama 3.3, Gemma 3 27B, Nemotron, etc.) — all failed to produce valid JSON

---

## Provider: SambaNova

**API**: OpenAI-compatible (`https://api.sambanova.ai/v1/chat/completions`)
**Auth**: Bearer token via `SAMBANOVA_API_KEY` env var
**JSON support**: `response_format: {type: "json_object"}` + schema in system prompt

### Account Status (Verified 2026-03-29)
- **$5 free credit** (30-day expiry), $0.30 used
- Payment method linked
- **Still on Free Tier rate limits** despite payment method
- SambaNova is undergoing a **billing system migration** — tier upgrades are stuck and require manual support intervention
- Community reports confirm this is a known issue: accounts stay at 20 RPD despite linked cards

### Rate Limits (Verified via API Headers)
All models show identical limits:

| Metric | Free Tier (Current) | Developer Tier (Target) |
|---|---|---|
| RPD per model | **20** | Unknown (docs say higher) |
| RPM | Unknown | Unknown |
| TPD | Unknown | 20M across all models |

**Per-model RPD is independent** — each model gets its own 20 RPD quota. With 13 models available, theoretical max is 260 requests/day by rotating models.

### Available Models (Verified 2026-03-29)

| Model | Context | Status | Pricing (per token) | $/job |
|---|---|---|---|---|
| DeepSeek-V3-0324 | 131K | Active | $3.00/$4.50 per 1M | $0.0086 |
| DeepSeek-V3.1 | 131K | Active | $3.00/$4.50 per 1M | $0.0086 |
| DeepSeek-V3.2 | 8K | Active | $3.00/$4.50 per 1M | $0.0086 |
| DeepSeek-V3.1-Terminus | 131K | Active | $3.00/$4.50 per 1M | $0.0086 |
| DeepSeek-V3.1-cb | 32K | Active | $0.15/$0.75 per 1M | $0.0005 |
| DeepSeek-R1-0528 | 131K | Active | $5.00/$7.00 per 1M | $0.0142 |
| DeepSeek-R1-Distill-Llama-70B | 131K | **410 GONE** | N/A | N/A |
| Meta-Llama-3.3-70B-Instruct | 131K | Active | $0.60/$1.20 per 1M | $0.0018 |
| Meta-Llama-3.1-8B-Instruct | 16K | Active | $0.10/$0.20 per 1M | $0.0003 |
| Llama-4-Maverick-17B-128E | 131K | Active | $0.63/$1.80 per 1M | $0.0020 |
| Qwen3-235B | 65K | Active | $0.40/$0.80 per 1M | $0.0012 |
| Qwen3-32B | 32K | Active | $0.40/$0.80 per 1M | $0.0012 |
| gemma-3-12b-it | 131K | Active | $0.20/$0.35 per 1M | $0.0006 |
| gpt-oss-120b | 131K | Active | $0.22/$0.59 per 1M | $0.0007 |
| MiniMax-M2.5 | 163K | **422 Error** | $0.30/$1.20 per 1M | N/A |
| E5-Mistral-7B-Instruct | 4K | Active (embedding) | $0.00/$0.13 per 1M | N/A |
| Llama-3.3-Swallow-70B | 131K | Active | $0.60/$1.20 per 1M | $0.0018 |

**Note**: DeepSeek-V3.2 has only 8K context — barely sufficient for our longest prompts (~3.4K tokens). All other models have ample context.

### Action Required
Contact SambaNova support to manually upgrade from Free to Developer tier.

---

## Provider: OpenRouter

**API**: OpenAI-compatible (`https://openrouter.ai/api/v1/chat/completions`)
**Auth**: Bearer token via `OPENROUTER_API_KEY` env var
**JSON support**: Varies by underlying model; free models generally poor at structured output

### Account Status (Verified 2026-03-29)
- **$10 credit purchased** — unlocked paid tier
- `is_free_tier: false` confirmed via `/api/v1/auth/key`
- `usage: 0` — no credits spent yet

### Rate Limits

| Metric | Free Tier (<$10 purchased) | Paid Tier ($10+ purchased) |
|---|---|---|
| RPD (free models) | 50 | **1,000** |
| RPM (free models) | 20 | 20 |
| RPD (paid models) | No hard limit | No hard limit |

**Important**: Free models are still subject to **upstream provider rate limiting** even with paid tier. During peak times, popular free models may be unavailable.

### Available Free Models (25 total, verified 2026-03-29)

**Promising for evaluation** (large context, capable models):
| Model | Context | Notes |
|---|---|---|
| meta-llama/llama-3.3-70b-instruct:free | 65K | Best open model, but 429'd in our tests |
| nousresearch/hermes-3-llama-3.1-405b:free | 131K | Largest free model available |
| nvidia/nemotron-3-super-120b-a12b:free | 262K | 120B MoE, large context |
| openai/gpt-oss-120b:free | 131K | GPT-derived open model |
| qwen/qwen3-coder:free | 262K | Qwen3 coder variant |
| qwen/qwen3-next-80b-a3b-instruct:free | 262K | Qwen3 MoE |
| google/gemma-3-27b-it:free | 131K | Gemma 3 27B |
| minimax/minimax-m2.5:free | 196K | MiniMax model |
| stepfun/step-3.5-flash:free | 256K | Step model |
| z-ai/glm-4.5-air:free | 131K | ZhipuAI model |

**Smaller/niche** (likely insufficient for scoring):
- arcee-ai/trinity-large-preview:free, arcee-ai/trinity-mini:free
- google/gemma-3-12b-it:free, google/gemma-3-4b-it:free, google/gemma-3n-e2b/e4b-it:free
- liquid/lfm-2.5-1.2b models, meta-llama/llama-3.2-3b:free
- nvidia/nemotron-nano models, openai/gpt-oss-20b:free

### Eval Results
**All OpenRouter free model evaluations returned 0% schema adherence.** This appears to be an integration issue — models return responses but our provider adapter may not correctly parse them, or the free model routing doesn't reliably support JSON mode. Needs investigation before OpenRouter is viable.

---

## Provider: Gemini (Google)

**API**: Google AI Studio REST API (`generativelanguage.googleapis.com/v1beta`)
**Auth**: API key via `GEMINI_API_KEY` env var
**JSON support**: `responseMimeType: "application/json"` with optional `responseSchema`

### Account Status (Verified 2026-03-29)
- Free tier API key active
- No billing linked

### Available Models on Free Tier

| Model | Status | RPM | RPD | Input Ctx | Output Ctx |
|---|---|---|---|---|---|
| gemini-2.5-flash-lite | **Active** | 15 | 1,000 | 1M | 65K |
| gemini-2.5-flash | **Active** | 10 | 250 | 1M | 65K |
| gemini-3-flash-preview | **Active** | ? | ? | 1M | 65K |
| gemini-3.1-flash-lite-preview | **Active** | ? | ? | 1M | 65K |
| gemini-2.0-flash | **Blocked** (limit: 0) | 0 | 0 | - | - |
| gemini-2.0-flash-lite | **Active** | ? | ? | 1M | 8K |
| gemini-2.5-pro | **Blocked** (limit: 0) | 0 | 0 | - | - |
| gemini-3-pro-preview | **Blocked** (limit: 0) | 0 | 0 | - | - |
| gemini-3.1-pro-preview | **Blocked** (limit: 0) | 0 | 0 | - | - |

**Notes**:
- Pro models are completely blocked on free tier (RPD = 0)
- Gemini 2.0 Flash also blocked (deprecated?)
- Flash-Lite models have the highest free limits (1,000 RPD)
- All active models support `responseMimeType: "application/json"` for structured output
- **No eval data yet** — Gemini provider adapter exists but hasn't been tested with Opus baseline
- Google reduced free tier quotas by 50-80% in Dec 2025

### Cost
**$0.00** — completely free on free tier. This is the best cost option if quality is acceptable.

### Gemini Paid Tier (for reference)
- Pay-as-you-go pricing available through Google Cloud billing
- Significantly higher limits but requires billing setup

---

## Provider: Ollama (Local)

**API**: Local REST API (`http://localhost:11434`)
**Auth**: None (localhost only)
**JSON support**: Via system prompt instructions (no native JSON mode in all models)

### Hardware
- **GPU**: NVIDIA GeForce RTX 4070 Ti SUPER (16 GB VRAM)
- **Quantization**: All models Q4_K_M
- **VRAM constraint**: 16 GB limits effective model size

### Installed Models

| Model | Disk | Params | VRAM Fit | Med. Lat | Eval r |
|---|---|---|---|---|---|
| qwen2.5:14b | 8.4 GB | 14.8B | Full GPU | 16.4s | **0.852** |
| qwen2.5:32b | 18.5 GB | 32.8B | Partial (CPU offload) | 116.0s | 0.817 |
| gemma3:27b | 16.2 GB | 27.4B | Tight fit | 106.3s | 0.681 |
| deepseek-r1:14b | 8.4 GB | 14.8B | Full GPU | 25.2s | 0.647 |
| qwen3:14b | 8.6 GB | 14.8B | Full GPU | 23.9s | 0.470 |

**Key observations**:
- **qwen2.5:14b is the clear local winner** — best correlation (0.852), fits entirely in VRAM, 16s latency
- qwen2.5:32b has marginally lower correlation but 7x slower due to CPU offload
- Models >16GB require CPU offload, causing massive latency increases
- **No rate limits, no cost** — process as many jobs as hardware allows
- **Throughput**: ~3.5 jobs/minute for qwen2.5:14b, ~0.5 jobs/minute for 32b models

### Cost
**$0.00** — electricity only (~$0.01/hour GPU at residential rates). Negligible.

### Resource Utilization (qwen2.5:14b)
- VRAM usage: ~8.5 GB of 16 GB
- Tokens/sec: ~40-60 tok/s output
- CPU: Minimal during inference (GPU-bound)
- Can run concurrently with normal desktop usage

---

## Provider: Anthropic (Current)

**API**: Anthropic SDK (custom, not OpenAI-compatible)
**Auth**: `ANTHROPIC_API_KEY` env var
**JSON support**: Native tool_use and structured output

### Current Production Usage
| Tier | Model | Purpose | $/job |
|---|---|---|---|
| Haiku | claude-haiku-3.5 | Fast filter scoring | $0.0029 |
| Sonnet | claude-sonnet-4.5 | Deep evaluation | $0.0109 |
| Opus | claude-opus-4.6 | Profile extraction, baselines | $0.0547 |

### Budget Gate
- Configurable monthly budget in config.yaml
- `cost_gate()` returns bool; callers decide whether to raise BudgetExceededError
- Free providers (gemini, ollama, ollm, openrouter, sambanova) bypass cost_gate

---

## Provider: Cohere

**API**: Cohere SDK
**Auth**: `COHERE_API_KEY` env var

### Eval Results
- **command-a-03-2025**: r=0.613, 100% schema, 16.1s median
- NOT_RECOMMENDED — poor correlation despite perfect schema adherence
- Not pursued further

---

## Provider: Mistral

**API**: OpenAI-compatible (`https://api.mistral.ai/v1`)
**Auth**: `MISTRAL_API_KEY` env var
**JSON support**: JSON mode on all models. Function calling supported.

### Free Tier ("Experiment")
- **2 RPM** across all models (the binding constraint)
- 500K TPM
- **1B tokens/month** (~363K jobs/month — effectively unlimited)
- Access to **Mistral Large** (128K context) — the flagship model
- Phone verification required for signup

### Eval Results
- **mistral-small-latest**: r=0.538, 100% schema, 5.8s median — NOT_RECOMMENDED
- **Mistral Large**: Untested — could be significantly better than mistral-small
- The 2 RPM limit makes batch evaluation slow (max 120 requests/hour) but the model quality could justify it
- Revisit: test Mistral Large with fewshot+opus baseline

---

## Provider: Groq (NEW)

**API**: OpenAI-compatible (`https://api.groq.com/openai/v1/chat/completions`)
**Auth**: API key from console.groq.com (no credit card required)
**JSON support**: JSON mode on 9/11 models. Structured Outputs (schema enforcement) on newer models.
**Inference**: Custom LPU hardware — sub-second latency on many models

### Free Tier Limits

| Model | Context | RPM | RPD | TPM | TPD | Jobs/day* |
|---|---|---|---|---|---|---|
| llama-3.3-70b-versatile | 128K | 30 | 1,000 | 12K | 100K | ~36 |
| llama-4-scout-17b-16e | 128K | 30 | 1,000 | 30K | 500K | ~181 |
| qwen/qwen3-32b | 131K | 60 | 1,000 | 6K | 500K | ~181 |
| moonshotai/kimi-k2-instruct | 131K | 60 | 1,000 | 10K | 300K | ~109 |
| openai/gpt-oss-120b | 128K | 30 | 1,000 | 8K | 200K | ~72 |
| llama-3.1-8b-instant | 128K | 30 | 14,400 | 6K | 500K | ~181 |

\*Jobs/day = min(RPD, TPD/2755). No credit card required.

### Key Notes
- Rate limits are per-organization, not per-key
- Cached tokens don't count toward limits
- 6K TPM on some models is tight — our requests average 2,755 tokens, so ~2 concurrent requests max
- Developer tier: ~10x limits, per-token pricing ($0.05-$1.00/M input)

---

## Provider: Cerebras (NEW)

**API**: OpenAI-compatible (`https://api.cerebras.ai/v1/chat/completions`)
**Auth**: API key from cloud.cerebras.ai (no credit card required)
**JSON support**: Schema enforcement on some models. Function calling on all models.
**Inference**: Wafer-Scale Engine — fastest inference available (~450-1800 tok/s)

### Free Tier Limits

| Model | Context | TPM | TPD | RPM | RPD | Jobs/day* |
|---|---|---|---|---|---|---|
| gpt-oss-120b | **131K** | 64K | **1M** | 30 | 14,400 | **363** |
| qwen-3-235b-a22b-instruct | ~32K | 60K | **1M** | 30 | 14,400 | **363** |
| llama3.1-8b | 8K | 60K | **1M** | 30 | 14,400 | **363** |
| zai-glm-4.7 | 131K | 60K | **1M** | 10 | 100 | 100 |

\*Jobs/day = min(RPD, TPD/2755). No credit card required.

### Key Notes
- **1M TPD is the highest free allowance of any provider** — 363 jobs/day per model
- gpt-oss-120b has **131K context** on free tier (no 8K limitation)
- Qwen3-235B context may be limited to ~32K on free tier (needs verification)
- Paid tier removes hourly/daily caps entirely
- Models rotate availability — check current catalog

---

## Provider: NVIDIA NIM (NEW)

**API**: OpenAI-compatible (build.nvidia.com)
**Auth**: API key (phone verification required)
**Free Tier**: ~40 RPM, 1K-5K startup credits
**Models**: Llama 3.3 70B, Mistral Large, Qwen3 235B, DeepSeek R1/V3.1, etc.
**Status**: Not yet tested. Worth investigating as supplementary provider.

---

## Per-Job Cost Comparison

Based on measured average: **2,532 input + 223 output tokens per job**

| Model | $/job | $/100 jobs | $/month (50/day) | Quality (r) |
|---|---|---|---|---|
| Gemini free tier | FREE | FREE | FREE | **Untested** |
| OpenRouter free models | FREE | FREE | FREE | 0% schema |
| Ollama/qwen2.5:14b | FREE | FREE | FREE | 0.852 |
| SN/Meta-Llama-3.1-8B | $0.0003 | $0.03 | $0.45 | Untested |
| SN/gemma-3-12b-it | $0.0006 | $0.06 | $0.88 | Untested |
| SN/gpt-oss-120b | $0.0007 | $0.07 | $1.03 | Untested |
| SN/Qwen3-235B | $0.0012 | $0.12 | $1.79 | **0.905** |
| SN/Llama-3.3-70B | $0.0018 | $0.18 | $2.68 | **0.935** |
| SN/Llama-4-Maverick | $0.0020 | $0.20 | $2.99 | 0.724 |
| Anthropic/Haiku 3.5 | $0.0029 | $0.29 | $4.38 | N/A (filter) |
| SN/DeepSeek-V3.x | $0.0086 | $0.86 | $12.90 | **0.934** |
| Anthropic/Sonnet 4.5 | $0.0109 | $1.09 | $16.41 | Baseline |
| SN/DeepSeek-R1-0528 | $0.0142 | $1.42 | $21.33 | Untested |
| Anthropic/Opus 4.6 | $0.0547 | $5.47 | $82.06 | Gold std |

---

## Rate Limit Summary

### Effective Daily Throughput (Verified)

| Provider | Daily Limit | RPM | Sufficient for 50 jobs/day? |
|---|---|---|---|
| **Ollama (local)** | Unlimited | ~3.5/min | YES |
| **Gemini 2.5 Flash-Lite** | 1,000 RPD | 15 RPM | YES |
| **Gemini 2.5 Flash** | 250 RPD | 10 RPM | YES |
| **OpenRouter (paid tier)** | 1,000 RPD | 20 RPM | YES |
| SambaNova (per model) | 20 RPD | ? | NO (need 3+ models) |
| SambaNova (rotating 3 models) | ~60 RPD | ? | YES (with scheduling) |

### SambaNova Model Rotation Strategy
With 20 RPD per model, rotating across top 3 models yields 60 RPD:
1. Meta-Llama-3.3-70B (20 jobs) — r=0.935
2. Qwen3-235B (20 jobs) — r=0.905
3. DeepSeek-V3.1 (10 jobs) — r=0.878

This covers 50 jobs/day but adds complexity and score variance across models.

---

## Local Hardware Profile

| Component | Specification |
|---|---|
| GPU | NVIDIA GeForce RTX 4070 Ti SUPER |
| VRAM | 16,376 MiB (16 GB) |
| GPU Util (idle) | ~1% |
| VRAM Used (idle) | 4,684 MiB (~4.6 GB, system/desktop) |
| VRAM Available | ~11,378 MiB (~11 GB) for models |
| Max Model Size (full GPU) | ~10 GB quantized (14B Q4_K_M models) |
| Models with CPU Offload | 16-18 GB (27B-32B Q4_K_M, 5-7x slower) |

---

## New Provider Research (Verified 2026-03-29)

### Tier 1: Highly Viable Free Providers

#### Groq (groq.com)
**API**: OpenAI-compatible (`https://api.groq.com/openai/v1/chat/completions`)
**Auth**: API key from console.groq.com (no credit card required)
**Inference**: Custom LPU (Language Processing Unit) hardware — ultra-fast

| Model | RPM | RPD | TPM | TPD | Jobs/day* | Notes |
|---|---|---|---|---|---|---|
| llama-3.3-70b-versatile | 30 | 1,000 | 12K | 100K | ~36 | TPD-limited |
| meta-llama/llama-4-scout-17b-16e | 30 | 1,000 | 30K | 500K | ~181 | Best throughput |
| qwen/qwen3-32b | 60 | 1,000 | 6K | 500K | ~181 | High RPM |
| openai/gpt-oss-120b | 30 | 1,000 | 8K | 200K | ~72 | Largest model |
| openai/gpt-oss-20b | 30 | 1,000 | 8K | 200K | ~72 | |
| moonshotai/kimi-k2-instruct | 60 | 1,000 | 10K | 300K | ~109 | Moonshot model |
| llama-3.1-8b-instant | 30 | 14,400 | 6K | 500K | ~181 | Smallest, fastest |

\*Jobs/day = min(RPD, TPD/2755)

**Key advantages**: No credit card needed, extremely fast inference, OpenAI-compatible, JSON mode support.
**Concerns**: TPD limits are the real constraint (not RPD). Llama 3.3 70B only gets 100K TPD = 36 jobs.
**Best model for our use case**: qwen3-32b or llama-4-scout (both 500K TPD = ~181 jobs/day).

#### Cerebras (cloud.cerebras.ai)
**API**: OpenAI-compatible (`https://api.cerebras.ai/v1/chat/completions`)
**Auth**: API key from cloud.cerebras.ai (free tier available)
**Inference**: Wafer-Scale Engine — fastest inference in the industry

| Model | TPM | TPH | TPD | RPM | RPH | RPD | Jobs/day* |
|---|---|---|---|---|---|---|---|
| llama3.1-8b | 60K | 1M | **1M** | 30 | 900 | 14,400 | **363** |
| qwen-3-235b-a22b-instruct | 60K | 1M | **1M** | 30 | 900 | 14,400 | **363** |
| gpt-oss-120b | 64K | 1M | **1M** | 30 | 900 | 14,400 | **363** |
| zai-glm-4.7 | 60K | 1M | **1M** | 10 | 100 | 100 | 100 |

\*Jobs/day = min(RPD, TPD/2755)

**Key advantages**: 1M tokens/day FREE per model (363 jobs/day!), fastest inference (~450 tok/s on 70B equivalent, 1800 tok/s on 8B). Qwen3-235B available, which scored r=0.905 on SambaNova.
**Concerns**: Free tier context window is **8,192 tokens**. Our prompts use ~2,500-3,400 input tokens + fewshot system prompt (~500 tokens) = ~3,000-3,900 tokens input. With 2K output, total is ~5,000-5,900. Should fit within 8K, but long JDs could be tight. Needs testing.
**Best model for our use case**: qwen-3-235b (if context sufficient) or gpt-oss-120b.

### Tier 2: Usable But Limited Free Tiers

#### Together AI (together.ai)
- **$100 free credits** for new users (requires $5 minimum purchase)
- 200+ open-source models (Llama 4, DeepSeek-V3, Qwen, Mixtral)
- Dynamic rate limits based on tier and capacity
- OpenAI-compatible API
- **Verdict**: Good value if willing to pay $5, but not truly "free tier"

#### Fireworks AI (fireworks.ai)
- **10 RPM** without payment method, $1 in starter credits
- 6,000 RPM with payment method
- Serverless inference with per-token pricing
- **Verdict**: Too limited on free tier (10 RPM) for batch evaluation

### Tier 3: Not Viable for Our Use Case

| Provider | Why Not |
|---|---|
| **DeepInfra** | No real free tier; unauthenticated requests IP-limited. Requires card for production. |
| **Cloudflare Workers AI** | 10,000 neurons/day free — opaque unit, likely insufficient. Edge-quantized models. |
| **Hyperbolic** | Limited model selection, unclear free tier stability |
| **Novita AI** | Pay-per-use only, no free tier |

---

## Recommendations

### Optimal Architecture (Cost-Minimized, Resilient)

**Primary Scoring Pipeline** (all free, cascading fallback):

| Priority | Provider | Model | Cost | Quality | Throughput | Role |
|---|---|---|---|---|---|---|
| 1 | **Cerebras** | qwen-3-235b | FREE | 0.905* | 363/day | Primary cloud |
| 2 | **Groq** | qwen3-32b | FREE | Untested | 181/day | Cloud fallback |
| 3 | **Gemini** | 2.5-flash-lite | FREE | Untested | 1,000/day | Cloud fallback |
| 4 | **Ollama** | qwen2.5:14b | FREE | 0.852 | Unlimited | Local fallback |
| 5 | **SambaNova** | Llama-3.3-70B | $0.0018 | 0.935 | 20/day | Paid fallback |

\*Correlation from same model family (Qwen3-235B) on SambaNova. Cerebras may differ due to context window or quantization.

**Why this order**:
- Cerebras has the best free limits (1M TPD) and hosts the model family with best quality
- Groq is the fastest backup with generous TPD (500K)
- Gemini has 1,000 RPD and is completely free
- Ollama is unlimited but slower (16s/job) and lower correlation
- SambaNova is paid but highest quality; use as premium fallback

**Daily capacity at 50 jobs/day**: Cerebras alone handles this with 313 jobs/day to spare. With all providers, total capacity exceeds 1,500 jobs/day.

### Immediate Action Items (Priority Order)

1. **Sign up for Cerebras** (cloud.cerebras.ai) — create account, get API key, build provider adapter
2. **Sign up for Groq** (console.groq.com) — create account, get API key, build provider adapter
3. **Test Cerebras qwen-3-235b** — run fewshot+opus eval, verify 8K context is sufficient
4. **Test Groq qwen3-32b and llama-3.3-70b** — run fewshot+opus eval
5. **Test Gemini models** — run fewshot+opus eval against gemini-2.5-flash-lite and gemini-3-flash-preview
6. **Fix OpenRouter integration** — debug 0% schema adherence
7. **Contact SambaNova support** — request manual Developer Tier upgrade (low priority now)

### Score Inflation Mitigation
All alternative models systematically inflate scores on poor-fit jobs. Options:
- **Linear recalibration**: Apply `adjusted_score = a * raw_score + b` fitted on Opus scores
- **Fewshot refinement**: Replace generic examples with actual Opus-scored jobs from DB
- **Score bucketing**: Map continuous scores to decision buckets (Apply/Maybe/Skip) which is the actual UX need

### Production Routing Design
```
Request -> Cerebras (primary, free, fast)
           |-- 429/timeout -> Groq (free, ultra-fast)
           |-- 429/timeout -> Gemini (free, reliable)
           |-- 429/timeout -> Ollama (free, local, slow)
           |-- all fail -> SambaNova (paid, last resort)
           |-- budget exceeded -> queue for next day
```
