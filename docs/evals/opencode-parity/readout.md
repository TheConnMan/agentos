# OpenCode vs Claude runner — parity readout (#313)

Evidence-only readout (AC3: no default-harness change). All numbers come from the
committed `results/*.jsonl`; the §2 tables are regenerated from those raw files by
`uv run python scripts/parity_eval.py --render --results-dir docs/evals/opencode-parity/results`
so tables and raw data cannot disagree.

## 1. Scope & method

Three arms run the same 5-case frozen-schema suite (`cases.json`) on the **same
substrate** — #312's rails-emulated `docker run` recipe (`--read-only`, tmpfs
`/tmp` + `/home/runner:uid=1000,gid=1000`, `--user 1000:1000`, `--cap-drop ALL`),
applied identically to both source-built images — graded with the platform's own
`EvalRunner` semantics (final text wins over deltas; a `classified-failure` final
never grades green; no-final falls back to joined deltas):

- **Arm A (baseline):** Claude image `agentos-runner:313`, **Anthropic direct** path
  (OAuth), `AGENTOS_MODEL=claude-sonnet-5`, credential = `CLAUDE_CODE_OAUTH_TOKEN`
  (subscription quota; no marginal USD).
- **Arm B (shipped default):** OpenCode image `agentos-runner-opencode:313`,
  OpenRouter `sk-or-` key, `AGENTOS_MODEL=z-ai/glm-4.6`.
- **Arm C (same-model OpenCode arm):** OpenCode image, OpenRouter, the **same model
  as Arm A** — `anthropic/claude-sonnet-5` exists on OpenRouter, so **no
  substitution was needed**.

**A↔C is not a clean harness-only isolation.** Arm A reaches Anthropic directly
(OAuth); Arm C reaches the same model through OpenRouter, so the **provider/routing
path changes together with the harness**. A↔C therefore isolates *harness + provider*,
not harness alone. Where a finding is nonetheless harness-attributable, this is
argued explicitly (e.g. bundle-tool availability in §4.3 cannot be a provider effect
because provider routing does not add or remove a bundle's MCP tool).

**N = 3 repetitions** per (arm × suite), cases in fixed order inside one fresh
container per rep. Small-N: pass counts are k/3 per case with no significance
claims; deltas are labeled "consistent" (e.g. 3/3 vs 0/3) or "noisy".

Full metadata (image SHAs, model ids, price table with source URL + date,
normalization rules) is in [`results/meta.json`](results/meta.json). Images were
source-built from `task/313-opencode-parity-evals` (published `:latest` lags this
stack). Run date: 2026-07-11.

**Normalization:** cost is **list-price-equivalent USD** from captured tokens
(`in*input_price + out*output_price`, OpenRouter price table in `meta.json`), not
billed spend. **Retries = 0 by construction** (the eval path has no retry loop);
`side_effect_flag` counts are the retry-suppression-relevant signal measurable here.
Tokens/model come from the runner's own OTel `gen_ai` spans (not Langfuse — its
async read-back is a known flake); a capture miss is a loud re-run, never a zero row.

**Honesty clause:** this is a 5-case synthetic suite (plain answer,
format-following, skill-token, MCP-nonce, multi-step). It supports per-use-case
deltas on these five behaviors and cannot claim coverage of real agent workloads.

**Data-quality caveats — the cost axis is not cleanly measurable here; read before the tables:**

1. **Arm A USD is understated (dropped cached input tokens).** The runner's OTel
   exporter records only `gen_ai.usage.input_tokens`/`output_tokens` and does **not**
   emit `cache_read_input_tokens`/`cache_creation_input_tokens`. On Claude, turns
   after the first bill substantial cached input tokens, so Arm A's captured input
   tokens (and thus its USD) are a **lower bound**. Fixing this is a runner-side
   change to `otel.py`, out of scope for this evidence-only issue (AC3).
2. **Arm C input-token capture is unreliable.** Every Arm C row reports
   `input_tokens = 2`, implausible for these prompts — when the OpenCode runner drives
   `anthropic/claude-sonnet-5` through OpenRouter, the per-step usage it receives
   under-reports prompt tokens (glm-4.6 on the same runner reports realistic counts,
   so this is specific to the Anthropic-via-OpenRouter usage payload). **Arm C's
   input-token and USD columns are not trustworthy;** its pass-rate, latency, and
   output-token columns are unaffected.
3. **Consequence:** because of (1), (2), and the A↔C provider confound, **this run
   does not support a clean cross-arm cost delta.** Per-case USD ratios are reported
   below as directional only, with the caveats attached.
4. **Arm A saw transient `rate-limit` classifications** on a few turns (OAuth quota
   under burst); the SDK recovered and produced `done` finals every time, so no
   neutral-case verdict changed. The `error_classification` field records where a
   transient was seen.
5. **`skill-token` fails on all three arms** for different reasons — see §4.3.

## 2. Per-use-case matrix

Regenerated from `results/*.jsonl` by `--render`. (Arm A USD is a lower bound and
Arm C input-tokens/USD are unreliable per §1.1–1.2.)

<!-- BEGIN RENDERED TABLES -->
#### `format-following`

| arm | pass k/n | median latency ms | tokens in/out (median) | USD (median) | tool calls | side-effect flags |
|---|---|---|---|---|---|---|
| A | 3/3 | 1483.71 | 32/8 | 0.000144 | 0 | 0 |
| B | 3/3 | 584.46 | 40/10 | 3.5e-05 | 0 | 0 |
| C | 3/3 | 2426.12 | 2/8 | 8.4e-05 | 0 | 0 |

#### `mcp-nonce`

| arm | pass k/n | median latency ms | tokens in/out (median) | USD (median) | tool calls | side-effect flags |
|---|---|---|---|---|---|---|
| A | 0/3 | 6441.55 | 58/240 | 0.002516 | 3 | 3 |
| B | 3/3 | 1248.62 | 40/24 | 5.9e-05 | 3 | 3 |
| C | 3/3 | 4826.09 | 2/45 | 0.000454 | 3 | 3 |

#### `multi-step`

| arm | pass k/n | median latency ms | tokens in/out (median) | USD (median) | tool calls | side-effect flags |
|---|---|---|---|---|---|---|
| A | 3/3 | 1694.13 | 32/9 | 0.000154 | 0 | 0 |
| B | 3/3 | 565.89 | 104/8 | 5.9e-05 | 0 | 0 |
| C | 3/3 | 2615.67 | 2/9 | 9.4e-05 | 0 | 0 |

#### `plain-answer`

| arm | pass k/n | median latency ms | tokens in/out (median) | USD (median) | tool calls | side-effect flags |
|---|---|---|---|---|---|---|
| A | 3/3 | 1389.89 | 2571/3 | 0.005172 | 0 | 0 |
| B | 3/3 | 1443.59 | 330/5 | 0.000151 | 0 | 0 |
| C | 3/3 | 3087.91 | 2/3 | 3.4e-05 | 0 | 0 |

#### `skill-token`

| arm | pass k/n | median latency ms | tokens in/out (median) | USD (median) | tool calls | side-effect flags |
|---|---|---|---|---|---|---|
| A | 0/3 | 3181.62 | 32/211 | 0.002174 | 0 | 0 |
| B | 0/3 | 3568.64 | 236/96 | 0.000269 | 7 | 0 |
| C | 0/3 | 8458.08 | 2/232 | 0.002324 | 9 | 1 |
<!-- END RENDERED TABLES -->

Per-case notes:

- **plain-answer / format-following / multi-step** — the three harness-neutral cases
  pass **3/3 on every arm**. No harness or model effect on outcome. (Arm A's
  `plain-answer` input tokens are high — 2571 — because the Claude system-prompt
  overhead dominates a one-line prompt; glm-4.6/B is 330.)
- **mcp-nonce** — the headline result: **A 0/3, B 3/3, C 3/3** (§3, §4.3).
- **skill-token** — **0/3 on all arms**, different failure modes per harness (§4.3).

## 3. Deltas vs baseline

**Outcome (pass-rate) — the robust axis.**

- **A ↔ C (same model `claude-sonnet-5`; harness + provider differ):**
  - **`mcp-nonce`: 0/3 (A) vs 3/3 (C) — CONSISTENT, and harness-attributable.** Same
    model, same prompt, same bundle. The provider path also differs (A↔C confound),
    but a provider does not add or remove a bundle's MCP tool — tool availability is a
    property of how the harness ingests the bundle. OpenCode's bundle compiler (#310)
    wires the bundle MCP; the Claude direct-plugin path does not surface it (§4.3).
  - `skill-token`: 0/3 vs 0/3 (both fail, different modes — §4.3).
  - The other three cases: 3/3 vs 3/3 — **no outcome effect.**
- **A ↔ B (shipped default: Claude/sonnet-5 vs OpenCode/glm-4.6):** `mcp-nonce`
  0/3 vs 3/3 (same availability effect as A↔C → harness, not model); `skill-token`
  0/3 vs 0/3; the three neutral cases 3/3 vs 3/3.

**Latency.** OpenCode arms boot ~2× slower (§4.2) but, once serving, are faster on the
short cases (e.g. `multi-step` B 566ms vs A 1694ms). Arm C is slowest per case
(OpenCode serve + the OpenRouter round-trip to Anthropic).

**Cost — directional only, NOT a clean delta (see §1.3).** Per-case A/B USD ratios
from the tables are `plain-answer` ≈34×, `mcp-nonce` ≈43×, `skill-token` ≈8×,
`format-following` ≈4×, `multi-step` ≈2.6× — i.e. **2.6× to 43×, highly
case-dependent, not a single "N× cheaper" number.** And even these are unreliable:
Arm A's USD is a lower bound (dropped cached tokens, §1.1) so the true A/B ratio is
*higher* than shown, while Arm C's USD is not trustworthy at all (§1.2). The only
sound cost statement this run supports: **glm-4.6 on OpenRouter (Arm B) is
materially cheaper per token than claude-sonnet-5, by a case-dependent multiple that
this setup cannot pin precisely.**

## 4. Failure modes

### 4.1 Steer (completion-deferred gap from ADR-0009)

Probe: start a long turn ("Count slowly from 1 to 40…"), inject a `/v1/steer` ("also
include the word PINEAPPLE") on the first delta, and check whether the current turn's
output (final-over-deltas, matching grading) contains PINEAPPLE. N=2 per harness
(`results/probes.jsonl`, `probe=steer`).

| harness | steer accepted | PINEAPPLE in current turn |
|---|---|---|
| Claude (A) | yes (2/2) | no (0/2) |
| OpenCode (B) | yes (2/2) | yes (2/2) |

- **Claude:** the steer POST is accepted (no 409), but the current turn completes its
  count without the steer marker appearing — the fast sonnet-5 generation had
  effectively committed before the mid-turn steer took effect.
- **OpenCode:** the steer marker appears in the current turn — one transcript streams
  `"…STEER-MARKER: PINEAPPLE…"` mid-response.

**Caveat (timing confound):** whether a mid-turn steer visibly alters the *current*
turn depends on how much generation remains when the steer lands, which is
generation-speed-dependent (sonnet-5 here finished the short count faster than glm-4.6
produced its preamble+count). So this probe shows the steer is *accepted* by both
harnesses but does **not** cleanly isolate a harness steer-semantics difference, and
it neither confirms nor refutes the ADR's completion-deferred-steer characterization.
A definitive probe needs a controlled long-running turn that cannot complete before
the steer lands. (An earlier version of this probe double-counted `Final` on top of
the streamed deltas, producing a spurious "the count repeats twice" artifact; the
collector was fixed to keep `Final` separate before these numbers were captured.)

### 4.2 Cold-start / no-resume

Container start → `/healthz` 200. **Measured by the driver harness bracketing
`docker run`** (timestamp captured before `docker run`, polled at 100ms until healthy;
`results/coldstart.jsonl`). The script's `--coldstart` mode now accepts `--start-ts`
so it can reproduce this container-start-to-ready measurement rather than timing only
its own poll loop.

| arm (image) | ready_ms (3 reps) | median |
|---|---|---|
| A — Claude | 2470, 2731, 1582 | **2470** |
| B — OpenCode/glm | 5524, 4937, 4639 | **4937** |
| C — OpenCode/sonnet | 4887, 4483, 4465 | **4483** |

The OpenCode image boots **~2× slower** — the window includes `opencode serve`
startup and the bundle compile, exactly the cost the second harness adds. **No
resume:** on the OpenCode image `AGENTOS_HISTORY_REF` is a startup `RuntimeError`
(refuses to silently cold-start a session the worker asked to rehydrate) — cited from
#312, not re-proven here.

### 4.3 Bundle skill/MCP surfacing (deny-by-default side-effect flagging)

The strongest cross-harness finding, from `mcp-nonce` and `skill-token`:

- **MCP (`mcp-nonce`):** OpenCode (B, C) surfaces the bundle's `agentos_probe` stdio
  MCP server (the #310 compiler wires it natively) — the model calls it and returns
  `NONCE-7f3a9c` → 3/3. The Claude image (A) does **not** surface it: the fixture's
  `plugin.json` declares MCP via the string-pointer form (`"mcpServers": ".mcp.json"`),
  which the Claude plugin path does not execute — the model, lacking the tool, calls
  other tools (3 `tool_note`s, all flagged side-effecting) and cannot produce the
  nonce → 0/3. **The `side_effect_flag` count is 3 on all three arms** — deny-by-default
  fires identically on both the Claude PascalCase allowlist and the OpenCode lowercase
  `OPENCODE_READONLY_TOOLS`; the pass difference is tool **availability**, not
  classification.
- **Skill (`skill-token`):** fails 0/3 everywhere, different modes:
  - Claude (A): `done` finals, but the `roundtrip-greeter` skill did not surface; the
    model confabulates a skill list and never emits `AGENTOS-ROUNDTRIP`.
  - OpenCode (B, C): the skill **does** load (the token appears in the deltas), but
    glm-4.6/sonnet then invoke a search tool that errors ("ripgrep execution failed"),
    ending the turn `classified-failure` (7–9 tool calls) so the final text is the
    error, not the token.
- **Fixture caveat:** the bundle fixture lives under `runner/tests/fixtures/` and is
  out of scope to change here (AC3). The MCP string-pointer declaration form is a known
  Claude-plugin-path silent-fail; a follow-up should fix the fixture's declaration form
  (or the Claude string-pointer path) and/or split neutral-vs-bundle suites so
  bundle-surfacing is measured separately from model quality.

### 4.4 USD-cap unenforced (observed)

The OpenCode entrypoint logs, at startup, that a positive daily-USD cap has no native
enforcement ("daily USD cap has no native OpenCode enforcement; the per-run
output-token ceiling remains enforced") — observed in every OpenCode container's logs,
including fake-model boots (#312 behavior). The per-run output-token ceiling *is*
enforced (§4.6). On the Claude path the daily USD cap is handed to the SDK.

### 4.5 Reasoning-model empty-final (#107) and reasoning-delta leakage

**Not observed on glm-4.6 (Arm B).** No graded row had an empty final on a `done`
status, and no reasoning-part text leaked into the ACI `text_delta` stream in any
transcript (the #226 spike's reasoning-delta-leak class did not appear on glm-4.6
through this runner). Both are reported as not-observed rather than absent by proof.

### 4.6 Budget-halt (clean parity, shared enforcement path)

Probe: re-run `multi-step` with `AGENTOS_BUDGET.max_output_tokens_per_run = 5`
(`results/probes.jsonl`, `probe=budget`):

| harness | final_status | classification | final text | pre-halt streamed output |
|---|---|---|---|---|
| Claude (A) | classified-failure | budget-exceeded | "run halted: output token budget exceeded" | none (0 chars) |
| OpenCode (B) | classified-failure | budget-exceeded | "run halted: output token budget exceeded" | none (0 chars) |

**Identical** on both harnesses, because the per-run output-token ceiling is enforced
by the **shared** `SessionRunner`/`BudgetTracker` path (`runner/src/agentos_runner/session.py`)
that both entrypoints construct — **not** by the Claude SDK (`build_options` passes no
token ceiling to `ClaudeAgentOptions`). The harnesses differ only in how they *feed*
usage into that shared tracker (Claude via SDK usage; OpenCode via #311's per-step
token summing); the ceiling check and the halt error+final pair are the same code.
The probe now captures the pre-`Final` delta transcript separately, and it was **empty
on both** — no partial answer streamed before the halt (the 5-token ceiling trips
before any answer delta). Only the *daily USD* cap differs (SDK-side on Claude,
unenforced on OpenCode — §4.4).

## 5. What this does not decide

Per **AC3**, this readout makes **no default-harness change** and no recommendation
masquerading as one. Findings only. What a future harness-default issue would need to
resolve from this evidence: (a) whether the OpenCode ~2× cold-start and the
bundle-MCP-surfacing win offset glm-4.6's behavior on richer suites; (b) fixing the
bundle fixture's MCP declaration form (or the Claude string-pointer path) so skill/MCP
cases measure harness behavior instead of a fixture artifact; (c) a trustworthy cost
comparison — this run cannot provide one until the runner OTel exporter captures
cached input tokens (§1.1), the Anthropic-via-OpenRouter input-token gap is closed
(§1.2), and the A↔C provider confound is removed by routing both harnesses through the
same provider; (d) a controlled steer probe that cannot complete before the steer
lands (§4.1). Open follow-ups are filed at PR time.
