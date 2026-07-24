# 21. Curie is a harness for coding agents: the CLI's primary user is Claude Code

Date: 2026-07-11
Status: Accepted

Status corrected from `Proposed` under
[ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md): the
positioning is built and is asserted as settled in the repo `README.md`
("Curie is a harness that runs the same immutable bundle..."). The agent-facing
CLI contract ships: `cli/src/exit.rs` (the `ExitClass` 0/1/2/3 classes),
`cli/src/guide.rs` (the `curie guide` primer), and
`cli/src/scaffold.rs` (which scaffolds a root `AGENTS.md` and an installable
`.claude/skills/using-curie/SKILL.md`). Later ADRs already extend it as a
settled premise (e.g. 0038).

## Context

Developers are building agents by pointing a coding agent (Claude Code, Codex,
Cursor) at the work, not by hand-writing every file. The interface to Curie is
therefore drifting away from the developer's own hands and toward the developer's
coding agent. In the limit, a human rarely types `curie` directly: they point
Claude Code at Curie and it authors the skills, wires the MCP servers, writes
the evals, and ships the bundle.

A coding agent can already write a `skill.md`. What it cannot do on its own is
guarantee that a skill working in a local chat behaves identically as a deployed
local process and again on Kubernetes. It authors the skill blind to how the
skill is deployed. That deployment-parity guarantee is exactly what Curie
already provides: the same immutable bundle and the same eval suite run across
tiers ([ADR-0014](0014-git-push-is-the-deploy.md)), on a substrate-agnostic core
([ADR-0012](0012-substrate-and-channel-agnostic-core.md)), local-first by
construction (the [vision](../vision.md)'s "the local loop is the production
loop"). The harness supplies the one thing the coding agent structurally can't
supply itself.

This forces a design question the earlier ADRs left implicit: **who is the CLI's
primary user, and what does the answer demand of every command?** If the answer
is "a coding agent," the CLI is a contract between a deterministic tool and a
non-deterministic consumer that may hallucinate commands, misread output, or skip
the docs. That consumer needs different affordances than a human at a prompt.

The evidence that this pays off is direct. Supabase shipped a ~100-line agent
primer (an Agent Skill) and measured task-correctness rise from 46% to 71% on one
model and 71% to 88% on another; their diagnosis was that agents "knew how to
implement it, they just didn't know when." The market lane is open: Google's
`agents-cli` and Supabase Agent Skills are among the only mature public examples
of onboarding the *agent* rather than the human developer.

## Decision

The CLI's primary consumer is a coding agent, driven by a developer. Curie is a
harness a coding agent uses to build agents properly. This is a positioning and
design commitment, not a single feature; it constrains how we build every
command.

1. **Agent-facing substrate is the default.** Commands emit structured output
   (JSON to stdout, human/log text to stderr), return semantic exit codes an
   agent can branch on (success vs. deterministic-input-error vs. transient), are
   non-interactive by default (an agent cannot answer a `y/n` prompt), prefer
   idempotent `ensure`-style verbs whose result is observable via a follow-up
   query, bound their output tokens, and return errors as recovery instructions
   rather than tracebacks.

2. **The harness is self-describing.** A single primer command emits a compact,
   self-contained "how to use this harness" document modeled on `SKILL.md`
   (roughly 100 lines, ordered by what the agent needs first), and `curie init`
   drops an installable Agent Skill plus an `AGENTS.md` into the target repo so
   coding agents auto-discover it through the standards they already scan. The
   primer carries only **non-discoverable** knowledge (the when/which decision
   logic and the landmines), never a directory tour the agent could derive itself
   — auto-generated restate-the-obvious guidance measurably hurts.

3. **The primer's spine is the parity ladder, made demonstrable.** It walks the
   agent through running the same bundle and the same `evals/cases.json` across
   `skill` (in-process runner), `local` (compose), and `cluster` (Kubernetes),
   and states the guarantee plainly: a tier-to-tier eval divergence is the harness
   catching a real environment bug, not the agent's skill logic. The first thing
   the agent experiences is the same eval passing in two substrates.

4. **Verify-first over trust-your-training.** The primer instructs the agent to
   confirm commands against the CLI's own machine-readable surface before running
   them, so it cannot invoke a command that does not exist. Stale model knowledge
   is corrected at runtime, on every invocation, rather than waiting on the next
   training cut.

5. **The interview is agent-conducted.** The "interview to scaffold an agent"
   experience is run by the coding agent against the human; the CLI stays
   non-interactive and deterministic, the agent materializes the answers as a spec
   file, and the CLI scaffolds from it. The judgment-laden conversation lives in
   the agent; the reproducible mechanics live in the CLI.

## Alternatives considered

- **Keep the CLI human-first and let agents cope.** Rejected: the interface is
  already drifting to the agent, and human-first affordances actively break agent
  use — a wizard that blocks on stdin is dead to an agent, prose errors are not
  actionable, and unstructured output forces the agent to parse guesswork. Human
  usability is preserved as a side effect (a good agent primer reads well to
  humans too), but it is no longer the primary constraint.
- **Rely on the model's training data to know how to use Curie.** Rejected: the
  Supabase result shows agents default to stale or absent training knowledge and
  skip reference docs even when fetch tools exist. A runtime-emitted primer is the
  only mechanism that corrects this on every invocation regardless of the model's
  training cut.
- **Expose the harness only as an MCP server, not a CLI.** Rejected as the
  primary surface: a CLI composes with the shell, git, and file muscle a coding
  agent already has, its invocations are reviewable in a transcript, and it is the
  one entry point this repo already commits to (`cli/CLAUDE.md`). An MCP surface
  can wrap the same commands later without changing this decision.
- **Drop "developer-first" for "agent-first."** Rejected: the developer still
  chooses Curie, owns the stack, and self-hosts it. What changes is the
  interface they operate it through, not who adopts it. Agent-operable is how the
  developer's chosen box gets driven, not a different audience.

## Consequences

- Every new command is designed for an agent consumer: structured output, semantic
  exit codes, non-interactive, idempotent, recovery-oriented errors. This becomes a
  review checklist and, where mechanical, a CI gate.
- The primer and the CLI grammar must not drift. The machine-readable command
  manifest ([#145](https://github.com/curie-eng/curie/issues/145)) is the
  verify-first surface and the primer's source of truth; a grammar change that does
  not regenerate it is a build failure.
- `curie init` scaffolding becomes the thing the agent is actually building, and
  ships an Agent Skill + `AGENTS.md` alongside the bundle, not a fixed example.
- Discoverability strategy shifts from generic "build agents" SEO to the parity
  pain ("my agent worked locally but broke deployed"), and publishing the harness's
  own before/after eval delta becomes both proof of value and the strongest
  citation asset for agent-driven discovery.
- This ADR is positioning; it does not by itself add a command. The commitments
  above are tracked as issues that reference it.
