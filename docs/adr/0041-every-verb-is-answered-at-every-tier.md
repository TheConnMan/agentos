# 41. Every verb is answered at every tier

Date: 2026-07-16

Status: Accepted

Implements [#459](https://github.com/curie-eng/curie/issues/459).

## Context

The CLI has three tiers: `skill` (a local runner container against a bundle on
disk), `local` (the compose stack and local platform API), and `cluster` (the
deployed Helm release). Issue #446 added the inspection and governance verbs
(`approvals`, `versions`, `memory`) to `local` and `cluster`. The `skill` tier
did not get them, so `curie skill versions` failed as a clap unknown
subcommand.

For a human that reads as "wrong tier, try another". For the CLI's primary
consumer, a coding agent, it is a dead end: an unknown subcommand is
indistinguishable from a typo, a version skew, or a verb that never existed. The
agent has no way to learn *why* the verb is missing here or *where* it does
exist, so it retries, guesses, or gives up.

Three of these concepts are genuinely different at the `skill` tier:

- **approvals** exists. The bundle manifest's `approvalPolicy` declares gates,
  and the runner arms them from the bundle plus the
  `CURIE_APPROVAL_REQUIRED_TOOLS` env override.
- **versions** does not exist, by construction. A version exists only because
  the platform assigns a `bundle_sha256` and a `version_label` at deploy time.
  `skill up` runs whatever bytes are on disk; there is no release to name.
- **memory** does not exist, by construction. `CURIE_MEMORY_REF` is never set
  at this tier, so the runner boots a `NullMemoryStore` and nothing persists.

The tempting shortcut for the latter two is to implement them as empty results:
`versions` prints no versions, `memory` prints no entries, both exit 0. That is
the worst option available. An empty success is a lie an agent cannot detect: it
reads as "this agent has no versions" when the truth is "versions are not a
thing here". A verb that lies is worse than one that is absent, because absence
at least fails loudly.

ADR-0021 froze the agent-facing exit-code contract at four classes: Success (0),
Failure (1), Usage (2), Transient (3). None of them fits. Usage says "fix your
input and retry" -- but no argv makes `skill versions` work. Failure says "the
operation did not succeed" -- but nothing failed; the request was well-formed and
correctly understood. Transient says "retry later" -- but no amount of waiting
creates a version at this tier. Squeezing this case into any of the four teaches
agents the wrong remediation.

## Decision

**Every verb is answered at every tier.** A verb either does the work, or
explicitly reports that the concept does not exist at this tier and names the
tier that has it. No verb at any tier may report a fabricated empty result for a
concept that does not exist there.

Concretely:

1. **`skill approvals` is implemented.** It reads the bundle's declared gates
   from `.claude-plugin/plugin.json` (falling back to `plugin.json`), mirroring
   the runner's `load_approval_policy` in two tiers that match its outcome while
   deliberately diverging in surface for one of them. If a REQUIRED key is
   missing (the manifest's `name`, or a gate's `gate`/`route`), the runner's
   pydantic parse raises, `load_approval_policy` catches it, and the runner arms
   ZERO gates, not merely the offending one. The CLI matches that outcome but
   will not report it as an empty gate list: an empty list reads as "no gates
   configured", which is a different lie than the truth, that the manifest is
   invalid and nothing is armed. So this tier surfaces a usage error instead,
   naming the parse problem and stating that the runner would arm zero gates. A
   key PRESENT but empty or whitespace is the second tier: it passes validation,
   and only that one gate is dropped by the runner's final comprehension filter
   while well-formed siblings stay armed; the CLI mirrors this exactly, skipping
   the empty gate and listing the rest. Because this tier's runner resolves its env
   once at container boot and there is no platform record to PATCH, `--gate` and
   `--clear` mutate nothing; they emit the `CURIE_APPROVAL_REQUIRED_TOOLS`
   assignment plus two caveats that keep it honest: the env applies only on a
   re-boot that forwards it by name (`curie skill up --secret
   CURIE_APPROVAL_REQUIRED_TOOLS`, since a plain `skill up` forwards only the
   model-credential names), and the runner *unions* the bundle's declared gates with the
   override, so `--gate` adds on top of them and `--clear` clears only the
   override. Omitting the second caveat would make the output lie by omission
   about which gates are actually armed.

2. **`skill versions` and `skill memory` are answered, not implemented.** They
   parse as known verbs and return an unavailable-with-reason error naming the
   alternative tier.

3. **`ExitClass::Unsupported = 4` is added to the ADR-0021 contract.** It means:
   the verb was understood, and the concept it inspects does not exist at this
   tier by construction. It is a distinct branch from the existing four because
   the agent's correct next action is distinct: not "fix the input" (Usage), not
   "the operation failed" (Failure), not "retry" (Transient), but "go to the tier
   that has this concept". The class is additive; the existing four keep their
   codes and meanings, so nothing branching on 0-3 changes behavior.

4. **The reason rides in the existing `{error, fix}` payload.** No new structured
   field. The `error` message names the absent concept and why it is absent; the
   `fix` names the cross-tier alternative. This keeps the `--json` error shape
   exactly two keys, as it is for every other class, so an agent parses one shape
   regardless of outcome. A structured field (e.g. `available_at: ["cluster"]`)
   is a strictly additive change later if machine-routing to the right tier turns
   out to need more than the prose hint.

ADR-0021 is immutable and stays as written; this ADR extends its exit-code
contract with the fifth class.

## Consequences

- An agent that runs a verb at the wrong tier now gets a deterministic exit 4
  plus a hint naming the right tier, instead of a clap error it cannot interpret.
  This is the difference between a dead end and a redirect.
- Exit 4 joins the frozen surface: consumers branching on exit codes must treat
  it as a distinct, non-retryable outcome. Anything that treats "non-zero" as
  failure keeps working unchanged.
- The bar for adding a verb to one tier is now higher: it must be answered at all
  three. That is the point -- the cost is paid once at authoring time, by the
  person who knows why the concept is absent, instead of repeatedly at runtime by
  every agent that guesses wrong.
- `skill approvals --gate` returning an env assignment rather than performing a
  mutation is a real asymmetry with `local`/`cluster`, where the same flag PATCHes
  the platform record. The output states this rather than hiding it. If the tier
  later grows a persistent runner config, the verb can mutate it and drop the
  caveats without changing the flag surface.
- `skill approvals --json` returns `{"gates": [{"gate", "route"}]}` while
  `local`/`cluster approvals --json` return `{"agent", "gated_tools": [...]}`.
  This divergence is deliberate, not an accident of two authors: the two tiers
  read different things. A bundle-declared gate is a manifest entry that names
  the `route` it fires on, so the route is part of the fact being reported; a
  platform gate is a tool name attached to a deployed agent record, where there
  is no route to name and the `agent` is what disambiguates the answer. Forcing
  one shape would mean either inventing a null `route` at the platform tier or
  dropping a real field at the skill tier, and both make the payload lie about
  what the tier knows. The cost is that an agent scripting this verb across tiers
  must key-sniff to learn which shape it got. Accepted: the shapes are stable and
  pinned by tests, so the sniff is a one-time branch rather than a moving target.
- The reason text lives in prose, so a machine cannot route on it without parsing
  English. Accepted for now: the exit code alone already tells an agent to stop
  retrying, which is the load-bearing signal.

## Known limitation: the CLI validates only the approval-relevant subset

`skill approvals` mirrors the runner's `load_approval_policy` closely enough to
match its two-tier validation and its last-route-wins deduplication, but it parses
only the approval-relevant slice of the manifest (`name`, `approvalPolicy`). The
runner validates the WHOLE `PluginManifest`. So a manifest that is well-formed in
its approval policy but invalid in an unrelated modeled field (for example
`commands: 123`) makes the runner's `model_validate` raise, `load_approval_policy`
return `{}`, and the runner arm ZERO gates -- while this view still lists the
declared gates as armed. That is the same lie-class this ADR exists to kill, and it
is open, not closed.

It is recorded rather than fixed because the obvious fix is the wrong one:
hand-mirroring all of `PluginManifest`'s fields in Rust would add a second ungated
mirror of a Python model, which is exactly the drift debt this repo already tracks
elsewhere. Closing it properly needs a shared or drift-gated manifest parser -- one
source of truth both tiers read, or a CI gate that fails when the two shapes
diverge. Until then the gap is live: the authoritative validation of a full
manifest is `plugin_format.validate_bundle`, which runs at deploy, not in this
verb.
