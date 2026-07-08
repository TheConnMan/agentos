# AgentOS quickstart

Welcome. This gets you from nothing to your first agent reply in about a minute,
with no credentials, no cluster, and no Slack. It runs the `skill` target: just
the runner container on your host Docker daemon, talking straight to the agent.

## Before you start

- **Docker** running locally.
- The **`agentos`** binary on your PATH. Grab it from
  [GitHub Releases](https://github.com/curie-eng/agentos/releases) (or build the
  CLI from `cli/` with `cargo build --release`).

## Your first agent reply

```bash
# 1. Scaffold a bundle. This creates a runnable weather skill you can edit later.
agentos init weather-bot
cd weather-bot

# 2. Boot the runner with the built-in fake model (offline, instant, no key).
agentos skill up --fake-model

# 3. Ask it something and watch the reply stream back.
agentos skill message "hello, are you there?"

# 4. Done. Tear the runner down.
agentos skill down
```

That is the full loop. `agentos skill up` starts the runner container,
`agentos skill message` sends a synthetic event and streams the reply, and
`agentos skill down` stops it. Edit `skills/weather-bot/SKILL.md` in the bundle
and re-run steps 2 and 3 to see your change answer.

## Level up: a real model

The fake model returns scripted replies. To get a genuine answer, bring your own
model through OpenRouter on the same `skill` path. Set `AGENTOS_CREDENTIALS` to
your OpenRouter key (it must arrive on that variable, not `ANTHROPIC_API_KEY`)
and name a model slug:

```bash
AGENTOS_CREDENTIALS="$OPENROUTER_TOKEN" agentos skill up \
  --image ghcr.io/curie-eng/agentos-runner:latest \
  --model z-ai/glm-5.2

agentos skill message "What's the weather in Paris? Answer in one short sentence."
agentos skill down
```

You should get a real answer instead of the canned loop.

## Going further

The `skill` loop is the fastest way to iterate on a plugin, but it is only the
runner. When you want the full platform (queue, worker, sandbox, traces):

- **`local`** runs the whole platform via docker compose on your machine.
- **`cluster`** runs it on Kubernetes as a Helm release, with tracing in
  Langfuse.

See [`docs/operations.md`](docs/operations.md) for the cluster runbook and
[`cli/README.md`](cli/README.md) for the complete command reference. The
[README](README.md#which-target-do-i-want) has a table to help you pick a target.
