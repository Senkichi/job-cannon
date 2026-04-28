# Topic 4: Confidence, Abstention, and "Missing Information" Handling

When a large language model (LLM) is used as a judge for an ordinal scoring task, its
output is structurally indistinguishable whether the model has examined a complete
piece of evidence or has guessed at a near-empty input. This section surveys the
literature on three coupled questions that bear directly on this failure mode: (i) how
to *elicit* a confidence signal alongside a judgment, (ii) how to *measure* the quality
of that signal, and (iii) how to design rubrics so a judge can *abstain* rather than be
forced into a default-valued numeric guess. The literature points consistently to one
conclusion: forcing a numeric ordinal output when the model lacks signal produces
predictably biased scores, and the remediation is some combination of explicit
abstention codes, calibrated verbalized confidence, and self-consistency aggregation.

## Verbalized confidence: from feasibility to caveat

The earliest demonstration that a model can produce a usable verbalized confidence
score is Lin, Hilton, and Evans's *Teaching Models to Express Their Uncertainty in
Words* [1]. Their core finding is that a fine-tuned GPT-3 can emit phrases such as
"90% confidence" or "high confidence" that map to empirically well-calibrated
probabilities, *without* using the model's logits. They introduce the CalibratedMath
suite as a test harness and show that the verbalized probability remains moderately
calibrated under distribution shift, and that the model is sensitive to its own
epistemic uncertainty rather than merely imitating human confidence patterns from the
training data. The paper's importance is methodological: it establishes that
"verbalized probability" — uncertainty as a generated token — is a real, learnable
signal and not just a stylistic artifact.

Tian et al. extended this finding into the era of RLHF-aligned models in *Just Ask for
Calibration* [2]. They benchmark ChatGPT, GPT-4, and Claude on TriviaQA, SciQ, and
TruthfulQA and report a counterintuitive but now widely-replicated result: for
RLHF-tuned models, *verbalized* confidences are typically better-calibrated than the
model's internal *conditional probabilities*, often reducing the expected calibration
error by a relative 50%. They further show that prompting the model to first generate
multiple candidate answers and then assign a confidence to each (a "top-k" verbalized
elicitation) sharpens calibration further. The implication for ordinal-judge design is
direct: when using an instruction-tuned model, the cleanest confidence channel is to
ask for it explicitly in the output schema rather than to rely on extracting it from
the API's logprobs — a path which is in any case unavailable on many commercial
endpoints.

Kadavath et al. study the same question from the *self-evaluation* angle in *Language
Models (Mostly) Know What They Know* [3]. They distinguish two confidence targets:
P(True) — the probability the model assigns that a previously-generated answer is
correct — and P(IK), the probability that the model "knows" the answer at all. Their
key positive finding is that larger models are well-calibrated on multiple-choice and
true/false formats when probed in the right format, and that P(True) increases
appropriately when supporting evidence is added to the context. The matching negative
finding is that P(IK) generalizes poorly to new tasks: a model can be calibrated about
its self-knowledge in-distribution while being miscalibrated out-of-distribution. This
caveat is structurally important for any system that scores a heterogeneous stream of
inputs (e.g., job descriptions whose verbosity, format, and content vary), because the
calibration measured on a training-like sample may not transfer.

Xiong et al.'s *Can LLMs Express Their Uncertainty?* [4] consolidates and partly
contradicts the optimistic Tian et al. result. Across five LLMs (including GPT-4 and
LLaMA 2 Chat) and five datasets, they find that black-box verbalized confidences are
*systematically overconfident* — the models imitate human-style certainty language
even when wrong. They decompose the elicitation pipeline into three knobs (prompting
strategy, sampling method, and aggregation across samples) and show that no single
prompt heuristic dominates; the most reliable variance reduction comes from
*aggregating across multiple samples*, i.e., self-consistency. The takeaway is that
"just ask" is necessary but not sufficient: a single verbalized score from a single
sample inherits the model's anchoring biases and should be smoothed by repeated
sampling.

A subsequent paper by Yang et al., *Calibrating Verbalized Probabilities for Large
Language Models* [5], analyses *why* verbalized probabilities are biased and proposes
explicit post-hoc rescaling of the discrete verbalized distribution before
calibration. Their work is mostly relevant as confirmation that even well-elicited
verbalized confidence has *systematic*, not merely random, bias and that the
correction is amenable to standard calibration machinery (temperature scaling and its
relatives) once the bias is characterized.

## Calibration metrics: ECE, Brier score, and what they hide

Three canonical metrics dominate the calibration literature. The Brier score, due to
Brier in the meteorological forecasting community [6], is the mean squared error
between predicted probabilities and binary outcomes. It is a strictly proper scoring
rule — it is uniquely minimized by reporting the true posterior — and it decomposes
into a calibration term and a refinement (resolution) term, which makes it useful for
diagnosing *whether* a model is biased versus uninformative. Its limitation is that
it scales with the marginal base rate, which makes cross-task comparisons fragile.

The Expected Calibration Error (ECE), as reported in modern deep-learning calibration
work, was popularized by Naeini, Cooper, and Hauskrecht in their work on Bayesian
Binning into Quantiles [7] and brought to the attention of the deep learning
community by Guo et al.'s *On Calibration of Modern Neural Networks* [8]. ECE is
defined by binning predictions by confidence and computing a weighted average of the
gap between bin accuracy and bin confidence. Guo et al.'s contribution beyond
metrology was the empirical demonstration that modern deep networks are systematically
*over*-confident — the very behavior later replicated for LLMs — and that a
single-parameter temperature-scaling fix recovers calibration cheaply on most
classification benchmarks. ECE is intuitive and trivially visualized as a reliability
diagram, but it is binning-dependent and insensitive to in-bin distribution shape; it
also does not directly measure refinement, only calibration. For LLM-as-judge work
the practical recommendation that emerges from [2], [4], and [8] is to report ECE
*and* either Brier score or a reliability diagram, since ECE alone can be near zero
for a model that always predicts the base rate.

A subtler point flagged by [4] is that for *verbalized* confidence, the support of the
distribution is discrete and concentrated on round numbers (50%, 70%, 90%, "high",
"medium"), so naive ECE binning aligns the bin edges with the model's preferred
output values, masking miscalibration. Calibration analyses on verbalized output
should either coarsen the bins or use a strictly proper score like Brier, which does
not require binning at all.

## Selective prediction, abstention, and the central-tendency failure mode

The right framing for a judge that may "lack signal" is *selective prediction* —
classification with a reject option. The deep-learning canonical reference is
Geifman and El-Yaniv's *Selective Classification for Deep Neural Networks* [9],
which shows how to attach a reject mechanism to any softmax classifier so the system
predicts only on inputs above a confidence threshold and abstains on the rest, with
formal coverage/risk guarantees. Their framework is silent on *how* the confidence
score is obtained; it only insists that one exists. Combined with the verbalized
confidence literature above, the obvious LLM analogue is: have the judge emit
(score, confidence) and route low-confidence items to a separate path (a re-fetch,
human review, or a "low_signal" bucket) rather than counting them as numeric
predictions.

The LLM-specific instantiation of selective prediction is the abstention literature.
Wen et al.'s TACL survey *Know Your Limits* [10] organizes the field across three
axes — the query, the model, and human-value alignment — and consolidates the
empirical finding that abstention reduces hallucination but is brittle and rarely
emerges from instruction tuning alone. Two concrete abstention training methods stand
out. Yang et al.'s *Alignment for Honesty* [11] constructs an honesty dataset by
substituting wrong or low-confidence responses with "I don't know" and fine-tunes on
it; the resulting models abstain more often without losing accuracy on questions they
*can* answer. Zhang et al.'s *R-Tuning* [12], which received an Outstanding Paper
Award at NAACL 2024, formalizes this as refusal-aware instruction tuning: the model
is trained on a refusal-aware dataset constructed from the *intersection* of training
questions with the model's own knowledge, so it learns when to defer rather than
confabulate. R-Tuning shows that the resulting "ability to abstain" generalizes as a
meta-skill across out-of-domain tasks, suggesting abstention is not just a per-domain
nicety but a transferable capability.

Kirichenko et al.'s *AbstentionBench* [13] is an important sobering counterpoint.
Across 20 datasets covering unanswerable questions, underspecification, false
premises, subjective interpretations, and outdated information, they evaluate 20
frontier LLMs and find that abstention is largely unsolved: model scale has almost
no effect, and — most surprisingly for design discussions — *reasoning fine-tuning
degrades abstention by ~24% on average*, including on math and science where reasoning
models are explicitly trained. A judge built on a reasoning-tuned model is therefore
*especially* prone to confabulating an ordinal score from thin context.

This is the literature's most direct treatment of the central-tendency / default-to-3
failure mode. When forced into a small ordinal scale (say, 1–5) without an explicit
abstention code, an LLM judge confronted with insufficient evidence will, like a
human respondent on a Likert survey, gravitate to the midpoint — the established
*central tendency bias* of survey methodology. The Likert-scale literature
characterizes the midpoint as both a "no-opinion" proxy and a risk-minimizing default
under uncertainty. The empirical AbstentionBench result extends this finding to LLM
judges: in the absence of a structurally accessible "I cannot judge" output, the model
emits a numerically central — and therefore *application-decisive* but
epistemically empty — score. The remediation is rubric-level: extend the output
schema with an explicit `INSUFFICIENT_INFORMATION` or `low_signal` code so the model
has somewhere to put genuine uncertainty other than the numeric midpoint.

## Self-consistency as a free confidence signal

Independently of verbalized elicitation, Wang et al.'s *Self-Consistency Improves
Chain of Thought Reasoning in Language Models* [14] established that sampling multiple
reasoning paths and majority-voting over the final answers improves accuracy on
arithmetic and commonsense benchmarks (gains up to +17.9% on GSM8K). The mechanism is
relevant beyond accuracy: the *agreement rate* across samples is itself a
self-consistency-derived confidence signal that requires no logprobs and no
verbalized confidence prompt. Xiong et al. [4] reports that consistency aggregation
across multiple samples is in fact the most reliable single technique for taming
verbalized overconfidence, generally outperforming any single prompt design. For an
ordinal judge this implies a hybrid design: sample N independent scorings, use the
inter-sample variance as an implicit confidence, and route high-variance items to the
abstention bucket.

## Synthesis and design implications

The literature converges on three points relevant to redesigning a forced-ordinal
LLM judge:

1. *Forced numeric output collapses to the midpoint when signal is absent.* The
   Likert central-tendency bias is the human analogue; AbstentionBench [13] shows
   the LLM analogue is real, severe, and resistant to scaling.
2. *The cleanest confidence channel for instruction-tuned models is verbalized,
   sampled, and aggregated.* Verbalized confidence beats logprobs for RLHF models
   [2], but a single sample is overconfident [4]; self-consistency aggregation [14]
   is the strongest single fix.
3. *Abstention should be a first-class output, not a downstream filter.* The
   abstention training literature [10], [11], [12] shows that an explicit
   `INSUFFICIENT_INFORMATION` slot in the rubric, paired with calibrated
   verbalized confidence on the numeric axes, makes the abstention pathway
   structurally available rather than backed into the midpoint.

For ECE-class evaluation specifically, the literature recommends pairing ECE with a
strictly proper score (Brier [6] or log-loss) and a reliability diagram — and, when
working with discrete verbalized confidences, coarsening bins or moving to Brier to
avoid the alignment artifact discussed by [4].

## References

[1] S. Lin, J. Hilton, and O. Evans. "Teaching Models to Express Their Uncertainty in Words." Transactions on Machine Learning Research, 2022. arXiv:2205.14334. https://arxiv.org/abs/2205.14334

[2] K. Tian, E. Mitchell, A. Zhou, A. Sharma, R. Rafailov, H. Yao, C. Finn, and C. D. Manning. "Just Ask for Calibration: Strategies for Eliciting Calibrated Confidence Scores from Language Models Fine-Tuned with Human Feedback." EMNLP, 2023. arXiv:2305.14975. https://arxiv.org/abs/2305.14975

[3] S. Kadavath, T. Conerly, A. Askell, T. Henighan, D. Drain, E. Perez, N. Schiefer, et al. "Language Models (Mostly) Know What They Know." Anthropic, 2022. arXiv:2207.05221. https://arxiv.org/abs/2207.05221

[4] M. Xiong, Z. Hu, X. Lu, Y. Li, J. Fu, J. He, and B. Hooi. "Can LLMs Express Their Uncertainty? An Empirical Evaluation of Confidence Elicitation in LLMs." ICLR, 2024. arXiv:2306.13063. https://arxiv.org/abs/2306.13063

[5] D. Yang, Y.-S. Tsai, and M. Yamada. "Calibrating Verbalized Probabilities for Large Language Models." 2024. arXiv:2410.06707. https://arxiv.org/abs/2410.06707

[6] G. W. Brier. "Verification of Forecasts Expressed in Terms of Probability." Monthly Weather Review, vol. 78, no. 1, pp. 1-3, 1950. https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml

[7] M. P. Naeini, G. F. Cooper, and M. Hauskrecht. "Obtaining Well Calibrated Probabilities Using Bayesian Binning." Proceedings of the AAAI Conference on Artificial Intelligence, 2015. https://ojs.aaai.org/index.php/AAAI/article/view/9602

[8] C. Guo, G. Pleiss, Y. Sun, and K. Q. Weinberger. "On Calibration of Modern Neural Networks." ICML, 2017. arXiv:1706.04599. https://arxiv.org/abs/1706.04599

[9] Y. Geifman and R. El-Yaniv. "Selective Classification for Deep Neural Networks." NeurIPS, 2017. arXiv:1705.08500. https://arxiv.org/abs/1705.08500

[10] B. Wen, J. Yao, S. Feng, C. Xu, Y. Tsvetkov, B. Howe, and L. L. Wang. "Know Your Limits: A Survey of Abstention in Large Language Models." Transactions of the Association for Computational Linguistics (TACL), 2024. arXiv:2407.18418. https://arxiv.org/abs/2407.18418

[11] Y. Yang, E. Chern, X. Qiu, G. Neubig, and P. Liu. "Alignment for Honesty." NeurIPS, 2024. arXiv:2312.07000. https://arxiv.org/abs/2312.07000

[12] H. Zhang, S. Diao, Y. Lin, Y. R. Fung, Q. Lian, X. Wang, Y. Chen, H. Ji, and T. Zhang. "R-Tuning: Instructing Large Language Models to Say 'I Don't Know'." NAACL (Outstanding Paper Award), 2024. arXiv:2311.09677. https://arxiv.org/abs/2311.09677

[13] P. Kirichenko, M. Ibrahim, et al. "AbstentionBench: Reasoning LLMs Fail on Unanswerable Questions." NeurIPS Datasets and Benchmarks Track, 2025. arXiv:2506.09038. https://arxiv.org/abs/2506.09038

[14] X. Wang, J. Wei, D. Schuurmans, Q. Le, E. Chi, S. Narang, A. Chowdhery, and D. Zhou. "Self-Consistency Improves Chain of Thought Reasoning in Language Models." ICLR, 2023. arXiv:2203.11171. https://arxiv.org/abs/2203.11171
