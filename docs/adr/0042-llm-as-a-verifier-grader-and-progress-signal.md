# 42. Adopt LLM-as-a-Verifier: a continuous-score semantic grader and a live progress signal

Date: 2026-07-14

Status: Accepted

Accepted 2026-07-17 by decision. The semantic verifier (`GraderKind.verifier`) is
adopted as the direction: it judges correctness where no string match exists,
which is the case a coding-agent bundle most often needs and the one a
deterministic grader structurally cannot cover. The **Validation gate** section
below is unmet at acceptance and `GraderKind.verifier` is not in the tree, so this
acceptance waives [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)'s
rule that a status is promoted only on evidence the decision is built. The gate's
substance still governs the implementing work
([#478](https://github.com/curie-eng/agentos/issues/478)), in particular the
unresolved model-plane question: continuous scoring needs scoring-token logprobs,
which the [ADR-0005](0005-claude-agent-sdk-adapter-and-frozen-aci.md)
claude-agent-sdk/Anthropic default does not expose.

Extends [ADR-0019](0019-freeze-eval-case-format.md) (the frozen eval-case format
and its `GraderKind` extension point), [ADR-0022](0022-eval-completeness-tier-parity-and-trace-promotion.md)
(eval completeness: grade the whole run, not the final text), and
[ADR-0004](0004-langfuse-observability-and-eval-backbone.md) (Langfuse as the
observability + eval backbone). Composes with
[ADR-0010](0010-approval-gates-and-human-in-the-loop.md) (approval/suspend) and
[ADR-0014](0014-git-push-is-the-deploy.md). Supersedes none. Motivated by
*LLM-as-a-Verifier: A General-Purpose Verification Framework* (Kwok et al.,
arXiv:2607.05391), which ships extensions for exactly our target harness
(Claude Code), making it directly relevant to [ADR-0021](0021-agentos-is-a-harness-for-coding-agents.md).

## Context

Two seams the earlier eval ADRs opened are still empty, and the same technique
fills both.

**1. There is no semantic grader, on purpose.** ADR-0019 froze the grader taxonomy
to `exact | contains | regex`, and its `models.py` says so in as many words: *"An
LLM-judge grader is a later addition; keeping these deterministic makes eval
results reproducible, which is the whole point of eval-as-CI."* ADR-0022 is adding
the *deterministic* trajectory-aware graders (a tool was called, a structured
result is present) and forwarding the full trajectory to the grader instead of
only `final.text`. Neither ADR gives us a grader that can judge whether an answer
is *semantically correct* when correctness is not a string match, which is the case
a coding-agent bundle most often needs and the one a deterministic grader
structurally cannot cover. The reason it was deferred is real: a naive LLM judge
is non-deterministic, which fights the reproducibility that makes eval-as-CI a
parity contract.

**2. A long-running agent has no early-warning signal.** ADR-0004 emits per-run
traces to Langfuse, and ADR-0021's whole pitch is unattended long-horizon coding
agents. But a run that is drifting toward failure looks, per-span, exactly like
one that is progressing until it commits broken state to disk (ADR-0014's push).
There is no scalar that says "this trajectory is getting worse."

The verifier paper resolves both with one mechanism, and resolves the
reproducibility objection specifically. Its core move: instead of prompting a
judge to emit a discrete score and taking the top token (resolution `1/G`, and a
**27% tie rate** on Terminal-Bench so distinct trajectories collapse to the same
score), it computes a **continuous** reward as the expectation over the
scoring-token distribution,

```
R(x, τ) = (1/CK) Σ_c Σ_k Σ_g  p_θ(v_g | x, c, τ) · φ(v_g)
```

averaged over `C` criteria and `K` repeated evaluations, then normalized to
`[0,1]`. This is what turns a noisy judge into a usable verifier: it yields **zero
ties** where the discrete judge ties 27%, and it exposes three independent knobs
that each attack a different error source. Granularity `G` (73.1% to 77.5% on
Terminal-Bench V2 at `G=1` to `20`), repeated evaluation `K` (variance shrinks
`O(1/K)`), and criteria decomposition `C` (for code agents:
**Specification / Output / Errors**, 75% to 78.3% ensembled). The same per-step
score tracks task progress: **Value-Order Correlation** (Spearman rank between step
index and score) is `0.848` on successful code-gen trajectories versus `0.769` on
failed ones. The score rises monotonically on success and stays flat on stall,
which is the early-warning signal we lack.

## Decision

**Adopt LLM-as-a-Verifier as one mechanism with two applications behind two
existing seams. Both land as opt-in; neither weakens the deterministic default.**

### Application A: a `verifier` grader kind (extends ADR-0019 / ADR-0022)

- Add `GraderKind.verifier` to the frozen eval-case schema
  (`apps/worker/src/agentos_worker/eval/models.py`), regenerating
  `eval-cases.schema.json` and the CLI's Rust mirror in the **same reviewed
  change** through ADR-0019's drift gate. The new grader carries a rubric
  (`criteria`, defaulting to the paper's Specification/Output/Errors triad), a
  verifier-model ref, and the scaling knobs `G` and `K` as bounded fields.
- The grader consumes the **full trajectory** ADR-0022 forwards (the `tool_note`
  and `side_effect_flag` frames the runner already emits), not only `final.text`.
  It is a whole-run semantic grader, which is the class ADR-0022's deterministic
  graders explicitly are not.
- It reports the continuous `R ∈ [0,1]` and a pass/fail thresholded on a
  per-case bound, so a suite still rolls up to the `N/M passed` line ADR-0019
  defines.

### Application B: a live verifier score in Langfuse + an optional gate (extends ADR-0004, composes with ADR-0010 / ADR-0014)

- Emit the per-step verifier score as a Langfuse metric on the run trace
  (ADR-0004's mapping layer), so the Runs view can show a trajectory's
  health curve and its VOC trend.
- A **policy gate** (ADR-0010's primitive, never platform-hardcoded) may pause a
  run when the score degrades past a bundle-configured bound. This is the first
  score-triggered use of the dormant suspend/resume path, and a pre-commit guard
  ahead of ADR-0014's push. It is bundle-owned policy, off by default.

### Reproducibility discipline (the objection ADR-0019 raised)

An LLM verifier is not bit-deterministic, so eval-as-CI reproducibility is earned,
not assumed:

- **Variance is controlled, not ignored.** Continuous scoring plus `K` repeated
  evaluations is the paper's own answer to run-to-run noise (`O(1/K)` variance),
  and a single-pass verifier already matches a `K=16` ensembled discrete judge.
- **The verifier run is stamped.** The verifier model id, `G`, `K`, and the
  criteria set fold into the per-run config hash ADR-0004 already requires, so a
  `verifier`-graded result is reproducible *given the same stamped verifier*, the
  same guarantee we give for a bundle version.
- **The default stays deterministic.** `exact | contains | regex` and ADR-0022's
  deterministic trajectory graders remain the recommended graders for CI-blocking
  cases. `verifier` is for the semantic cases they cannot cover, and a suite
  states which of its cases are verifier-graded.

### Validation gate (before this moves to Accepted)

Per ADR-0001 this project's ADRs are evidence-driven. This one is Proposed until a
spike shows, on a real bundle: (1) a `verifier` grader agreeing with human
pass/fail on a held-out case set at a useful rate, (2) the run-to-run score
variance at the chosen `K` small enough that a threshold does not flap in CI, and
(3) the **model-plane path** below actually resolved.

## Alternatives considered

- **Train a task-specific reward model instead.** Rejected for the same reason the
  paper gives: a trained reward model is constrained by its training data and does
  not generalize across a coding-agent bundle's open-ended tasks, whereas the
  verifier is training-free and plug-and-play.
- **Ship best-of-N candidate selection now (the paper's TurboAgent /
  Probabilistic Pivot Tournament).** Deferred, not adopted. TurboAgent is a
  drop-in Claude Code proxy that dispatches `N` candidate trajectories and selects
  the winner via PPT (`O(N²)` to `O(Nk)`), and it is squarely ADR-0021's altitude.
  But our runner is one-process-per-sandbox running **one** trajectory
  ([ADR-0005](0005-claude-agent-sdk-adapter-and-frozen-aci.md)); `N` parallel
  trajectories is an `N×` cost and a concurrency-kernel change (the sacred module,
  `apps/worker/CLAUDE.md`). It earns its own ADR once the grader and progress
  signal are proven, and the verifier is the shared substrate it would build on.
- **A discrete LLM-judge grader (top-token score).** Rejected as the primary form:
  the 27% tie rate and coarse `1/G` resolution are exactly what make a judge
  unreliable for ranking and threshold-flappy in CI. Continuous scoring is the
  cheap fix and it is the same number of model calls.

## Consequences

- **Model-plane coupling is the load-bearing constraint.** The continuous score
  needs scoring-token logprobs, and the paper's prompt uses a letter-scale to
  extract them. Our default harness is claude-agent-sdk on Anthropic
  (ADR-0005), which does **not** expose scoring-token logprobs. So the verifier
  must run on a logprob-capable model, which ties this to the OpenRouter
  ([#24](https://github.com/curie-eng/agentos/issues/24)) and local-model plane,
  **or** use the paper's Appendix B.6 two-stage workaround for frontier models
  without logprobs. The Proposed ADR does not pick one; the spike must, and the
  choice is a follow-up decision an amendment records.
- A new `GraderKind` is a frozen-contract change: schema regen plus CLI mirror
  plus drift gate in one change (ADR-0019), and old bundles are unaffected (they
  name no `verifier` grader).
- Verifier grading costs one-or-more extra model calls per graded case (`C·K`
  scoring passes), so it is opt-in per case and unattractive for cases a cheap
  deterministic grader already covers, which is the intended split with ADR-0022.
- The progress-signal application is additive to ADR-0004's trace schema and does
  not change the ACI; the optional gate rides ADR-0010's existing primitive rather
  than adding a new lifecycle state.
- Beyond eval and monitoring, the same continuous score is a dense RL reward in the
  paper (`1.8×` sample efficiency off-policy, `1.1×` on GRPO). We record that as
  known future headroom, not scope here, since AgentOS trains no policies today.
