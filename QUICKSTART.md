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

## Still have time? Run your skill on your local box or on a Kubernetes cluster

The `skill` loop is just the runner container. To see your skill run through the
real product path (queue, worker, sandbox, traces), the same way a Slack mention
would, put the full platform in front of it. Two options, lightest first.

### On your local box (Docker stack)

`local` brings up the whole platform with docker compose (API, worker, backing
stores, and the console UI) and runs your skill through it. Same runner, now with
the queue and worker in front, all on your machine. Run these from your bundle
directory:

```bash
agentos local up                      # start the full compose stack
agentos local deploy --plugin-dir .   # push the bundle to the local API on :28000
agentos local message "What's the weather in Paris?"
agentos local down                    # tear it all down
```

Watch the run land in the console UI at `http://localhost:28080/?api=1`. Like
`skill`, the local stack runs the fake model by default, so replies are scripted
until you wire a real model.

> If `local up` prints `agentos-ui-1 is unhealthy`, ignore it: that is a known
> UI healthcheck quirk. The stack is fine and the UI answers on `:28080`.

### On a Kubernetes cluster (Helm)

`cluster` installs the platform as a Helm release and drives your skill on real
Kubernetes, with runs traced in Langfuse. The short arc:

```bash
agentos cluster up                    # install the platform (Helm release)
kubectl port-forward svc/agentos-api 8000:8000 -n agentos &   # deploy needs this
agentos cluster deploy --plugin-dir . # push the bundle to the in-cluster API
kill %1
agentos cluster message "What's the weather in Paris?"
agentos cluster down --yes            # uninstall
```

For a real model on the cluster, the runner model key is
`--set agentSandbox.runner.model=<slug>` (not `runner.model`, which silently
no-ops). The full cluster runbook, including credentials, web egress, and the
Langfuse login, is in [`docs/operations.md`](docs/operations.md).

See [`cli/README.md`](cli/README.md) for the complete command reference, and the
[README](README.md#which-target-do-i-want) for a table to help you pick a target.
