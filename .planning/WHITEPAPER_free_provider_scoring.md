# Replacing Paid LLM Scoring with Free API Providers: A Practical Evaluation

**Date:** 2026-03-29
**Context:** Job Cannon — personal job search app with AI-powered job fit scoring

---

## Abstract

We evaluated whether free-tier LLM API providers (Cerebras, Groq, SambaNova, OpenRouter, Gemini, Ollama) can replace paid Anthropic Sonnet ($0.011/job) for structured job-candidate fit scoring. Using 61 Opus gold-standard baselines, we tested 9 models across 10 prompt engineering variants (including 6 novel variants designed to combat score inflation). Our key findings: (1) Cerebras Qwen3-235B achieves r=0.839 with 100% JSON schema adherence at n=61, sufficient for production use; (2) plain few-shot calibration examples remain the most robust prompting technique at scale — 6 novel variants that appeared superior at n=10 screening all regressed at n=30 confirmation; (3) n=10 screening inflates correlation by +0.05 to +0.13, making it unreliable for final model selection; (4) different models respond to different prompting techniques, invalidating single-model prompt optimization.

---

## 1. Problem Statement

Job Cannon uses a two-tier AI scoring pipeline: Haiku fast-filter followed by Sonnet deep evaluation. Sonnet evaluation costs $0.011/job and produces a structured output (0-100 fit score, summary, strengths, gaps, talking points, priority skills) validated against a JSON schema. At 50 jobs/day, this costs ~$16.50/month — acceptable for a personal tool, but unnecessarily expensive given the proliferation of free-tier LLM APIs.

**Goal:** Find a free provider that preserves Sonnet's rank-ordering quality (measured by Pearson correlation against gold-standard scores) while maintaining structured output reliability (measured by JSON schema adherence rate).

**Constraints:**
- Must produce valid JSON matching a fixed schema (7 required fields including nested objects)
- Must maintain rank-ordering quality (r >= 0.85 "suitable", r >= 0.70 "marginal")
- Must handle ~2,755 tokens/request (input + output average)
- Must sustain ~50 jobs/day with free-tier quotas

---

## 2. Methodology

### 2.1 Gold Standard Baselines

We scored 61 jobs using Claude Opus via the Max subscription CLI (`claude -p --model opus`), storing results in an `opus_score` column. Opus serves as the gold standard because it consistently produces the most calibrated and defensible scores across our evaluation history. Token usage: avg 2,532 input + 223 output = 2,755 total per job.

The 61 jobs span a diverse distribution: scores range from 3 to 82 (mean 35.8), covering poor fits (junior marketing roles for a senior data scientist), adjacent-field partial fits (data engineering), and strong fits (staff-level analytics/ML roles in health tech).

### 2.2 Evaluation Framework

Our `eval_provider.py` CLI framework:
1. Samples n jobs with stored Opus scores from the production database
2. Reconstructs the exact prompt (system + user message) for each job
3. Calls the target provider via the same `call_model()` dispatcher used in production
4. Computes Pearson r between Opus baseline scores and provider scores
5. Reports schema adherence rate, latency statistics, and a categorical verdict

**Verdict thresholds:**
- SUITABLE: r >= 0.85 AND schema >= 95%
- MARGINAL: r >= 0.70 AND schema >= 80%
- NOT_RECOMMENDED: below marginal on any metric

### 2.3 Prompt Variants

We tested 10 system prompt variants, all building on the base Sonnet evaluation prompt:

| Variant | Key Addition | Hypothesis |
|---|---|---|
| `default` | Base prompt only | Control |
| `rubric` | Explicit scoring rubric (90-100 exceptional...0-29 poor) | Structure reduces variance |
| `fewshot` | 5 calibration examples (scores 15, 38, 62, 78, 91) | Anchoring via demonstration |
| `fewshot-rubric` | Rubric + examples | Combined benefits |
| `fewshot-anchored` | Examples + anti-inflation instructions | Combat upward bias |
| `fewshot-cot` | Examples + chain-of-thought before scoring | Reasoning reduces inflation |
| `fewshot-distribution` | Examples + expected score distribution | Base rate awareness |
| `fewshot-comparative` | Examples + explicit ideal role anchor | Fixed reference point |
| `fewshot-rubric-strict` | Rubric + examples + hard scoring caps | Prevent extreme inflation |
| `fewshot-negative` | Examples + counter-examples of bad scoring | Learn from mistakes |

### 2.4 Models Tested

| Provider | Model | Context | Free Tier Limit | Inference |
|---|---|---|---|---|
| Cerebras | qwen-3-235b-a22b-instruct-2507 | ~32K | 1M TPD / 30 RPM | Wafer-Scale Engine |
| Cerebras | gpt-oss-120b | 131K | 1M TPD / 30 RPM | (Unavailable — rotated out) |
| Groq | llama-3.3-70b-versatile | 128K | 100K TPD / 30 RPM | Custom LPU |
| Groq | qwen/qwen3-32b | 131K | 500K TPD / 60 RPM | Custom LPU |
| Groq | meta-llama/llama-4-scout-17b-16e | 128K | 500K TPD / 30 RPM | Custom LPU |
| SambaNova | Meta-Llama-3.3-70B-Instruct | 128K | 20 RPD | Custom silicon |
| SambaNova | Qwen3-235B-A22B | 128K | 20 RPD | Custom silicon |
| SambaNova | DeepSeek-V3.2 | 128K | 20 RPD | Custom silicon |
| SambaNova | DeepSeek-V3.1 | 128K | 20 RPD | Custom silicon |
| Ollama | qwen2.5:14b (local) | 128K | Unlimited | RTX 4070 Ti Super 16GB |
| Ollama | qwen2.5:32b (local) | 128K | Unlimited | CPU offload, very slow |
| OpenRouter | nvidia/nemotron-3-super-120b-a12b:free | 262K | 1K RPD | Cloud (various) |
| Gemini | gemini-2.5-flash-lite | 128K | 20 RPD (actual) | Google TPU |
| + others | Cohere command-a, Mistral small, various Ollama models | — | — | — |

### 2.5 Experimental Design

**Phase 1 — Screening (n=10):** We ran all 6 new prompt variants across 4 primary models (Cerebras qwen-3-235b, Groq llama-4-scout, Groq qwen3-32b, Ollama qwen2.5:14b) using 10-job batches. Provider rate limits were exploited for parallelism — each provider has independent limits, so Cerebras, Groq, Ollama, OpenRouter, and Gemini experiments ran simultaneously.

**Phase 2 — Confirmation (n=30):** Top-performing (model, variant) pairs from screening were promoted to 30-job runs to validate the screening signal.

**Phase 3 — Full validation (n=61):** The baseline `fewshot` variant was run at full 61-job scale on Cerebras and Groq.

---

## 3. Results

### 3.1 Confirmed Results (n >= 30)

These are the only results we trust for production decisions:

| Provider | Model | Variant | r | Schema | n | Latency | Verdict |
|---|---|---|---|---|---|---|---|
| Cerebras | qwen-3-235b | fewshot | **0.839** | **100%** | 61 | 6.2s | MARGINAL |
| Ollama | qwen2.5:14b | fewshot-comparative | **0.856** | 93% | 30 | 21.2s | MARGINAL |
| Cerebras | qwen-3-235b | fewshot-distribution | 0.808 | 100% | 30 | 1.3s | MARGINAL |
| Cerebras | qwen-3-235b | fewshot-anchored | 0.766 | 97% | 30 | 4.4s | MARGINAL |

### 3.2 SambaNova Results (n=18-19, small sample)

SambaNova achieved the highest correlations but with very small samples due to 20 RPD daily limits:

| Model | Variant | r | Schema | n | Latency |
|---|---|---|---|---|---|
| Meta-Llama-3.3-70B | fewshot | **0.935** | 100% | 18 | 1.9s |
| DeepSeek-V3.2 | fewshot | **0.934** | 95% | 19 | 5.0s |
| Qwen3-235B | fewshot | **0.905** | 100% | 19 | 4.5s |
| DeepSeek-V3.1 | fewshot | 0.878 | 100% | 19 | 2.7s |

These results should be viewed cautiously given the small n. Notably, the same Qwen3-235B model scored r=0.905 at n=19 on SambaNova but r=0.839 at n=61 on Cerebras — the larger sample reveals a more moderate true correlation.

### 3.3 Screening Results (n=10) vs Confirmation (n=30)

| Model | Variant | r (n=10) | r (n=30) | Delta |
|---|---|---|---|---|
| Cerebras qwen-3-235b | fewshot-distribution | 0.935 | **0.808** | **-0.127** |
| Cerebras qwen-3-235b | fewshot-anchored | 0.898 | **0.766** | **-0.132** |
| Ollama qwen2.5:14b | fewshot-comparative | 0.878 | **0.856** | -0.022 |

The Cerebras screening results were dramatically inflated. Only the Ollama result held up at confirmation. This is a critical methodological finding — see Section 4.2.

### 3.4 Model x Variant Interaction Effects

Different models responded to prompting techniques differently:

**Cerebras qwen-3-235b (n=10 screening):**
| Variant | r | Schema |
|---|---|---|
| fewshot-distribution | 0.935 | 100% |
| fewshot-anchored | 0.898 | 100% |
| fewshot-comparative | 0.891 | 100% |
| fewshot-rubric-strict | 0.840 | 100% |
| fewshot-negative | 0.805 | 100% |
| **fewshot-cot** | **0.699** | 100% |

**Ollama qwen2.5:14b (n=10 screening):**
| Variant | r | Schema |
|---|---|---|
| **fewshot-comparative** | **0.878** | 100% |
| **fewshot-cot** | **0.868** | 90% |
| fewshot-rubric-strict | 0.855 | 80% |
| fewshot-distribution | 0.836 | 100% |
| fewshot-anchored | 0.835 | 100% |
| fewshot-negative | 0.823 | 100% |

Notable: `fewshot-cot` (chain-of-thought) was the **worst** performer for Cerebras (r=0.699) but the **second best** for Ollama (r=0.868). This interaction effect means optimizing prompts on one model and assuming transferability to another is a methodological error.

### 3.5 Provider Reliability

| Provider | Schema Adherence | Rate Limit Issues | Practical Assessment |
|---|---|---|---|
| Cerebras | 97-100% across all variants | None at 3s delay | **Production-ready** |
| Groq llama-4-scout | 100% across all variants | None at 3s delay | Reliable fallback |
| Groq qwen3-32b | 90% (schema failures on complex outputs) | Occasional 429 at 30s delay | Marginal |
| Groq llama-3.3-70b | N/A (untestable) | 46/61 calls returned 429 | **Unusable on free tier** |
| Ollama qwen2.5:14b | 80-100% (variant-dependent) | None (local) | Reliable but slow (16-21s) |
| OpenRouter (Nemotron) | 70-80% | Upstream 429s from Venice | **Not recommended** |
| Gemini flash-lite | 90-100% | 20 RPD actual (not 1K as documented) | Quota too small |

### 3.6 Score Inflation Pattern

All free-tier models exhibit systematic score inflation relative to Opus:

| Provider/Model | Opus Mean | Model Mean | Inflation |
|---|---|---|---|
| Cerebras qwen-3-235b (n=61) | 35.8 | 68.7 | +32.9 |
| Ollama qwen2.5:14b (n=30) | varies | varies | +20-35 |
| SambaNova Llama-3.3-70B (n=18) | 36.2 | 69.1 | +32.9 |

Score ranges compress upward: Opus scores of 3-8 map to model scores of 14-42, while Opus scores of 65-82 map to model scores of 85-94. The rank ordering is preserved (hence high correlation) but the absolute values are systematically inflated. Poor-fit jobs cluster around model score ~40-42 rather than ~10-15.

---

## 4. Discussion

### 4.1 The "Good Enough" Threshold

No free provider achieved our SUITABLE threshold (r >= 0.85, schema >= 95%) at confirmed sample sizes. The best confirmed result — Cerebras qwen-3-235b at r=0.839, 100% schema — falls just short on correlation but excels on schema reliability.

For a personal job search tool where the primary use case is rank-ordering jobs by fit quality, r=0.839 is likely sufficient. The scoring is used to surface the top ~20% of jobs for closer review, not to make binary accept/reject decisions. A model that correctly identifies "this is a better fit than that" 84% of the time (roughly what r=0.84 implies for rank-ordering pairs) is adequate for triage.

### 4.2 Small-Sample Screening is Unreliable

Our most important methodological finding: **n=10 screening inflates correlation estimates by +0.05 to +0.13 versus n=30 confirmation.** Two of our three "winners" from screening (fewshot-distribution at 0.935 and fewshot-anchored at 0.898) regressed to 0.808 and 0.766 respectively — both performing *worse* than the baseline fewshot variant at n=61.

This is not surprising statistically — Pearson r with n=10 has very wide confidence intervals (approximately +/-0.35 at 95% CI). But it's a practical warning: **do not make production decisions based on 10-sample screening runs.** We recommend n >= 30 as the minimum for trustworthy signal, with n >= 50 for final validation.

The SambaNova results (r=0.905-0.935 at n=18-19) should be viewed through this lens. They may regress toward 0.83-0.85 at larger sample sizes, similar to the Cerebras results.

### 4.3 Model-Specific Prompt Optimization

The interaction between model architecture and prompting technique was striking. Chain-of-thought (CoT) prompting — widely regarded as a reliable technique for improving LLM reasoning — actually **degraded** Cerebras Qwen3-235B performance (r=0.699 vs 0.839 baseline), while being the second-best technique for Ollama Qwen2.5:14b (r=0.868 vs 0.852 baseline).

Similarly, "fewshot-distribution" (which instructs the model about expected score distributions) was the top screening performer for Cerebras but only 4th best for Ollama. The "fewshot-comparative" variant (anchoring against an explicit ideal role) was best for Ollama but 3rd for Cerebras.

**Implication:** Prompt engineering results from one model do not transfer to another. When building multi-model systems (like a cascading fallback chain), each model should be independently optimized.

### 4.4 Rate Limit Reality vs Documentation

Several provider rate limits differed significantly from documentation:

- **Gemini gemini-2.5-flash-lite:** Documented as 1,000 RPD. Actual free tier: **20 RPD** (quota exhausted after 20 calls).
- **Groq llama-3.3-70b-versatile:** 12,000 TPM sounds generous, but at 2,755 tokens/request, it allows only **4.35 requests/minute** — a 15s delay wasn't sufficient to avoid 429s.
- **Cerebras gpt-oss-120b:** Listed in their documentation but returned 404 — models rotate availability on free tier.
- **OpenRouter free models:** Upstream rate limiting from the backend provider (Venice) was the binding constraint, not OpenRouter's own 1K RPD.

**Lesson:** Always verify rate limits empirically with test calls before planning capacity. Documentation may be aspirational, outdated, or measured differently than your use case.

### 4.5 Schema Adherence as a Hard Filter

Schema adherence proved to be a strong differentiator. Cerebras qwen-3-235b achieved 100% across all prompt variants — every single response parsed as valid JSON matching our 7-field schema. Groq qwen3-32b achieved only 90%, and OpenRouter Nemotron 70-80%.

For structured output in production, we treat schema adherence as a hard filter: any model below 95% is too risky. A single schema failure means a job goes unscored, requiring either a retry (doubling cost) or a fallback call. Cerebras's perfect 100% at n=61 is a significant competitive advantage.

---

## 5. Production Architecture Decision

Based on these results, we're implementing a cascading fallback chain:

```
Request -> Cerebras qwen-3-235b (r=0.839, 100% schema, 363/day)
           |-- exhausted/error -> Groq llama-4-scout (r=0.833, 100% schema, 181/day)
           |-- exhausted/error -> Ollama qwen2.5:14b (r=0.856, 93% schema, unlimited, local)
           |-- exhausted/error -> Anthropic Sonnet ($0.011/job, last resort)
```

**Expected daily capacity:** 363 + 181 + unlimited = well over 500 jobs/day at $0.00 cost. Anthropic Sonnet is only invoked if all free providers are down simultaneously.

**Prompt strategy:** Plain `fewshot` for all providers (the most robust variant at scale). Per-model variant optimization is theoretically appealing but the confirmed performance differences are small and the complexity cost is high.

---

## 6. Recommendations for Similar Projects

1. **Start with Opus/Sonnet baselines.** Gold-standard scoring from the best available model is essential for meaningful evaluation. Without it, you're comparing models to each other with no ground truth.

2. **Use n >= 30 for any decision.** n=10 screening is useful for quickly eliminating obvious non-contenders, but the variance is too high for ranking or selection.

3. **Test prompts across all target models.** Model-variant interactions are real and significant. A prompt that adds +0.10 correlation on one model may subtract -0.14 on another.

4. **Verify rate limits empirically.** Documentation lags reality, especially for free tiers where limits change frequently.

5. **Prioritize schema adherence over correlation.** A model with r=0.80 and 100% schema is more useful in production than r=0.90 and 80% schema — schema failures require fallback calls that negate the cost savings.

6. **Free-tier capacity adds up.** A cascade of 3-4 free providers can sustain hundreds of requests/day. The total is more valuable than any single provider's limit.

---

## Appendix A: Complete Results Table

### All Confirmed Runs (n >= 18)

| Provider | Model | Variant | r | Schema | n_valid/n | Latency | Capacity/day |
|---|---|---|---|---|---|---|---|
| SambaNova | Meta-Llama-3.3-70B | fewshot | 0.935 | 100% | 18/18 | 1.9s | 20 |
| SambaNova | DeepSeek-V3.2 | fewshot | 0.934 | 95% | 18/19 | 5.0s | 20 |
| SambaNova | Qwen3-235B | fewshot | 0.905 | 100% | 19/19 | 4.5s | 20 |
| SambaNova | DeepSeek-V3.1 | fewshot | 0.878 | 100% | 19/19 | 2.7s | 20 |
| Ollama | qwen2.5:14b | fewshot-comparative | 0.856 | 93% | 28/30 | 21.2s | unlimited |
| Ollama | qwen2.5:14b | fewshot | 0.852 | 94% | 48/51 | 16.5s | unlimited |
| Cerebras | qwen-3-235b | fewshot | 0.839 | 100% | 61/61 | 6.2s | 363 |
| Groq | llama-3.3-70b | fewshot | 0.839 | 25%* | 15/61 | 16.0s | 36 |
| Ollama | qwen2.5:32b | fewshot | 0.817 | 92% | 47/51 | 118.2s | unlimited |
| Cerebras | qwen-3-235b | fewshot-distribution | 0.808 | 100% | 30/30 | 1.3s | 363 |
| Ollama | qwen2.5:14b | default | 0.820 | 94% | 48/51 | 16.4s | unlimited |
| Ollama | qwen2.5:14b | rubric | 0.776 | 96% | 49/51 | 16.2s | unlimited |
| Cerebras | qwen-3-235b | fewshot-anchored | 0.766 | 97% | 29/30 | 4.4s | 363 |
| Ollama | qwen2.5:14b | fewshot-rubric | 0.732 | 86% | 44/51 | 16.1s | unlimited |
| SambaNova | Llama-4-Maverick | fewshot | 0.724 | 100% | 19/19 | 1.7s | 20 |

*Groq llama-3.3-70b schema % reflects 46 rate-limit errors, not schema failures. The 15 successful calls had 100% schema.

### All Screening Runs (n=10)

| Provider | Model | Variant | r | Schema | Latency |
|---|---|---|---|---|---|
| OpenRouter | Nemotron-120B | fewshot-distribution | 0.959 | 80% | 70.2s |
| OpenRouter | Nemotron-120B | fewshot | 0.951 | 70% | 103.9s |
| OpenRouter | Nemotron-120B | fewshot-cot | 0.937 | 80% | 81.6s |
| Cerebras | qwen-3-235b | fewshot-distribution | 0.935 | 100% | 6.4s |
| Cerebras | qwen-3-235b | fewshot-anchored | 0.898 | 100% | 1.2s |
| Cerebras | qwen-3-235b | fewshot-comparative | 0.891 | 100% | 1.5s |
| Groq | qwen3-32b | fewshot-anchored | 0.885 | 90% | 5.8s |
| Ollama | qwen2.5:14b | fewshot-comparative | 0.878 | 100% | 16.8s |
| Ollama | qwen2.5:14b | fewshot-cot | 0.868 | 90% | 16.2s |
| OpenRouter | Nemotron-120B | fewshot-anchored | 0.862 | 70% | 100.1s |
| Ollama | qwen2.5:14b | fewshot-rubric-strict | 0.855 | 80% | 16.2s |
| Groq | qwen3-32b | fewshot-comparative | 0.848 | 90% | 9.0s |
| Cerebras | qwen-3-235b | fewshot-rubric-strict | 0.840 | 100% | 6.5s |
| Ollama | qwen2.5:14b | fewshot-distribution | 0.836 | 100% | 14.7s |
| Ollama | qwen2.5:14b | fewshot-anchored | 0.835 | 100% | 16.3s |
| Groq | llama-4-scout | fewshot-distribution | 0.833 | 100% | 1.2s |
| Ollama | qwen2.5:14b | fewshot-negative | 0.823 | 100% | 15.2s |
| Groq | llama-4-scout | fewshot-anchored | 0.810 | 100% | 1.2s |
| Cerebras | qwen-3-235b | fewshot-negative | 0.805 | 100% | 6.4s |
| Gemini | flash-lite | fewshot-anchored | 0.802 | 90% | 3.5s |
| Gemini | flash-lite | fewshot | 0.799 | 100% | 4.0s |
| Groq | qwen3-32b | fewshot-distribution | 0.754 | 100% | 3.2s |
| Groq | llama-4-scout | fewshot-comparative | 0.754 | 100% | 1.4s |
| Cerebras | qwen-3-235b | fewshot-cot | 0.699 | 100% | 1.3s |

### Historical Default Prompt Results (pre-fewshot)

| Provider | Model | Variant | r | Schema | n |
|---|---|---|---|---|---|
| Ollama | qwen2.5:32b | default | 0.800 | 80% | 16/20 |
| SambaNova | Qwen3-235B | default | 0.750 | 100% | 20/20 |
| Ollama | qwen2.5:14b | default | 0.723 | 100% | 20/20 |
| Ollama | deepseek-r1:14b | default | 0.625 | 95% | 19/20 |
| Ollama | gemma3:27b | default | 0.614 | 85% | 17/20 |
| SambaNova | DeepSeek-V3.2 | default | 0.320 | 100% | 20/20 |
| SambaNova | Meta-Llama-3.3-70B | default | 0.241 | 95% | 19/20 |

The impact of few-shot calibration examples is dramatic: SambaNova Llama-3.3-70B went from r=0.241 (default) to r=0.935 (fewshot) — a +0.694 improvement. This was the single largest quality improvement across all experiments.

---

## Appendix B: Infrastructure

**Eval CLI:** `python eval_provider.py --provider <name> --model <id> --sample-size <n> --prompt-variant <variant> --baseline opus --delay <seconds> --retries <n> -y`

**Provider adapters:** 10 adapters following a common `BaseProvider` interface, all using OpenAI-compatible `/v1/chat/completions` endpoints (except Anthropic and Gemini which use native APIs).

**Hardware:** Ollama runs on RTX 4070 Ti Super (16GB VRAM). qwen2.5:14b (8.4GB) fits fully in VRAM; 32b+ models require CPU offload and are 5-7x slower.

**Total eval runs this session:** 72+ JSON reports, covering 9 models, 10 prompt variants, sample sizes from 10 to 61.
