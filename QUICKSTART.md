# AgentOS quickstart

Welcome. This gets you from nothing to your first agent reply in about a minute,
with no credentials, no cluster, and no Slack. It runs the `skill` target: just
the runner container on your host Docker daemon, talking straight to the agent.
For the 30-second version, see the [README Quickstart](README.md#quickstart). To
go further — a real local model, the full local/cluster runbooks, or working on
AgentOS itself — see [Where to go next](#where-to-go-next).

## Before you start

- **Docker** running locally.
- The **`agentos`** binary on your PATH. One command downloads the prebuilt
  binary for your platform, verifies its signed checksum, and installs it:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/curie-eng/agentos/main/get-agentos.sh | bash
  ```

  It always verifies the sha256 and runs `cosign verify-blob` when cosign is on
  PATH (`AGENTOS_REQUIRE_COSIGN=1` requires it). To run the download-verify-install
  steps by hand instead, see
  [`docs/release-verification.md`](docs/release-verification.md#verify-the-cli-before-installing-it).
  Working on AgentOS itself? Run that same script from a source checkout and it
  builds the CLI instead of downloading it — see
  [Where to go next](#where-to-go-next).

## Your first agent reply

1. **Scaffold a bundle.** This creates a starter skill named for your agent,
   plus an `AGENTS.md` and a `using-agentos` harness-primer skill, that you edit
   next.

   ```bash
   agentos init my-agent
   cd my-agent
   ```

2. **Boot the runner** with the built-in fake model — offline, instant, no key.

   ```bash
   agentos skill up --fake-model
   ```

3. **Ask it something** and watch the reply stream back.

   ```bash
   agentos skill message "hello, are you there?"
   ```

4. **Done.** Tear the runner down.

   ```bash
   agentos skill down
   ```

That is the full loop. `agentos skill up` starts the runner container,
`agentos skill message` sends a synthetic event and streams the reply, and
`agentos skill down` stops it. Edit `skills/my-agent/SKILL.md` in the bundle
and re-run steps 2 and 3 to see your change answer.

A committed first-party example lives at `examples/weather/`: `cd
examples/weather && agentos skill up` runs it from a clean clone. For the
"engine as an in-bundle stdio MCP server" shape — a bundle that ships its own
tools as a stdio subprocess the harness spawns — see the template at
[`examples/text-stats-engine/`](examples/text-stats-engine/README.md).

## Level up: a real model

The fake model returns scripted replies. For a genuine answer, drop
`--fake-model` and export a credential first (`agentos` forwards it into the
runner container), then re-run `skill up`. Any one of `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`, or `AGENTOS_CREDENTIALS` works for the Anthropic default:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...
agentos skill up
agentos skill message "What's the weather in Paris? Answer in one short sentence."
agentos skill down
```

(`ANTHROPIC_API_KEY` or `AGENTOS_CREDENTIALS` work in place of
`CLAUDE_CODE_OAUTH_TOKEN`, as noted above.)

To use a different provider or model instead of the Anthropic default, bring
your own model through OpenRouter on the same `skill` path. Set
`AGENTOS_CREDENTIALS` to your OpenRouter key (it must arrive on that variable,
not `ANTHROPIC_API_KEY`, and here it selects OpenRouter specifically because
it's paired with `--image`/`--model`) and name a model slug:

```bash
AGENTOS_CREDENTIALS="$OPENROUTER_TOKEN" agentos skill up \
  --image ghcr.io/curie-eng/agentos-runner:latest \
  --model z-ai/glm-5.2

agentos skill message "What's the weather in Paris? Answer in one short sentence."
agentos skill down
```

You should get a real answer instead of the canned loop.

## Where to go next

The `skill` loop is just the runner container. From here:

- **A real local model, no Anthropic key** — the opt-in offline `--local-model`
  demo (a real model in a container over an Anthropic-compatible endpoint, model
  sizing, and gotchas): [`docs/local-model.md`](docs/local-model.md).
- **Run your bundle on the full local platform** — the queue, worker, sandbox,
  and traces, the same path a Slack mention takes, via docker compose
  (`agentos local up` → `local deploy` → `local message`). Walkthrough and the
  ticket-verification loop in
  [`docs/onboarding.md`](docs/onboarding.md#the-real-product-loop-with-local).
- **Run it on Kubernetes** — the platform as a Helm release
  (`agentos cluster up` → `cluster deploy` → `cluster message`). Full runbook,
  including credentials, web egress, and the Langfuse login, in
  [`docs/operations.md`](docs/operations.md).
- **Working on AgentOS itself** — the repo-checkout dev stack, tests, and
  from-scratch walkthrough in [`docs/onboarding.md`](docs/onboarding.md) and
  [`AGENTS.md`](AGENTS.md). One-command bootstrap from a clone:
  `./get-agentos.sh` (builds the CLI, then runs `agentos install` for deps and
  the runner image).
- **Every command and flag** — the complete reference in
  [`cli/README.md`](cli/README.md); the
  [README target guide](README.md#which-target-do-i-want) has a table to help
  you pick `skill` vs `local` vs `cluster`.
