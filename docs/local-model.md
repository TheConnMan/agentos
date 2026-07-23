# Offline demo: a real local model (no Anthropic key)

`--local-model` is an opt-in offline path that runs a real local model through an
Anthropic-compatible endpoint, so the demo answers for real and can drive a 1-2
tool-call loop with no Anthropic key. This is a DEMO / dev-loop path, NOT the
production agent path — the built-in fake model stays the zero-dependency default
(see the [QUICKSTART](../QUICKSTART.md)).

Use the flag on whichever target you are running:

```bash
agentos skill up --local-model
agentos local up --local-model
agentos cluster up --local-model
```

Bare `--local-model` uses `qwen3:4b`. Override it by passing a model name:

```bash
agentos local up --local-model qwen3-coder:30b
```

Combine `--minimal` with `--local-model` when you want the core local loop plus
Ollama, without Langfuse or the UI:

```bash
agentos local up --minimal --local-model
```

## How it runs

`skill up` and `local up` run the model in a Docker container and point spawned
runners at that endpoint. Both the `skill up --local-model` and compose paths
persist the pulled model in a Docker volume, so a re-up is fast and does not
re-download the model; the skill-path volume is named `<container>-ollama-data`
(the compose path uses `ollama_data`) and can be reclaimed with
`docker volume rm <volume>`. `cluster up` uses the in-chart inference Deployment;
the chart renders the Ollama Service and Deployment, opens the runner egress
carve-out automatically, and bakes `ANTHROPIC_BASE_URL` plus the inference model
into the runner template.

## Choosing a model

| Model | Loaded (Q4) | Min box | Notes |
|---|---|---|---|
| qwen3:4b | ~2.5GB | 8GB | demo default; clears the 1-2 tool-call bar |
| qwen3-coder:30b | ~17-19GB | 32GB | MoE 30B/3.3B-active; real agentic-coding upgrade |
| gemma4:e4b | ~5GB | 16GB | "4.5B effective" name understates RAM; needs Ollama >=0.31.x |

Gotchas: Ollama 0.24.0 fails `gemma4` with `unknown model architecture`; qwen3
works on 0.24.0 and gemma4 needs >=0.31.x. Gemma HF repos are gated and return
HTTP 400 on `hf.co/google/...`; use a non-gated mirror such as
`hf.co/unsloth/gemma-4-E4B-it-GGUF:<quant>`. RAM sizing tracks the loaded
footprint, not the "effective params" marketing number.
