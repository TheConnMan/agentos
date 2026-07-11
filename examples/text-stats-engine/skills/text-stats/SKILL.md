---
name: text-stats
description: Compute word count, character count, or reading time for a piece of text using the in-bundle text-stats engine. Invoke whenever the user asks how long a passage is, how many words or characters it has, or how long it takes to read.
allowed-tools:
  - text-stats-engine
---

# Text statistics

## When to run
The user asks how many words or characters a passage has, or how long it takes
to read.

## How to answer
1. Take the text the user provided (or asks about).
2. Call the in-bundle engine's tool for the metric requested:
   - `word_count` for a word total.
   - `char_count` for a character total (pass `include_whitespace: false` to
     exclude spaces).
   - `reading_time_minutes` for an estimated read time at 200 wpm.
3. Report the number plainly, naming the metric and the text it applies to.

## Notes
The engine runs as an in-bundle stdio MCP server (`scripts/engine_server.py`,
declared in `.mcp.json`). The harness spawns it as a subprocess; there is no
network service and nothing to provision. See this bundle's README for when to
prefer this shape over a CLI subprocess.
