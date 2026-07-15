# github-issues — an agent on an authed, off-the-shelf MCP server

An example bundle for the **"authenticated third-party MCP server"** shape: the
agent's tools come from a server we do **not** write — the off-the-shelf
[`@modelcontextprotocol/server-github`](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
stdio server — and reaching the service needs a **secret** (a GitHub personal
access token). It exists to prove the end-to-end path for *any* authed MCP
integration: declare the server in `.mcp.json`, and forward its credential into
the sandbox at launch with `agentos skill up --secret <NAME>`.

## What's here

```
github-issues/
  .claude-plugin/plugin.json    bundle manifest
  .mcp.json                     declares the off-the-shelf GitHub stdio server
  skills/github-issues/SKILL.md  a skill that reads and triages issues
```

There is no server code in this bundle — that is the point. `.mcp.json` points
`command` at `mcp-server-github`, the binary the reference server package
installs. The runner image pre-installs that package
(`runner/Dockerfile`: `npm install -g @modelcontextprotocol/server-github`), so
the server starts with **no runtime network fetch**. The GitHub token is not in
the bundle; it is forwarded by name at launch (below).

## How the secret reaches the server

`agentos skill up --secret GITHUB_PERSONAL_ACCESS_TOKEN` forwards the variable
**by name** into the runner container — docker reads its value from your
environment, so the token never appears in argv. Inside the sandbox the GitHub
server reads `GITHUB_PERSONAL_ACCESS_TOKEN` from the environment (the `.mcp.json`
`env` block also maps it explicitly). This is the same by-name forwarding the
CLI already uses for model credentials; `--secret` just extends it to a bundle's
own MCP secrets.

## Run it end-to-end (manual)

For the interactive path, run `agentos`, choose **Explore examples**, then
**GitHub issues**. AgentOS starts the runner, keeps the entire conversation in
its TUI, and stops the runner when you leave the chat.

Prerequisites: the runner image built once (`agentos build`), a model credential
in your environment (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`), and a
GitHub PAT — a read-scoped (`public_repo` / `repo:read`) token is enough to list
and read issues.

```bash
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_token_here

cd examples/github-issues

# Optional: confirm the server binary is present and loads in an offline check.
# It runs --network none and forwards no secret, so if the GitHub server
# refuses to start without a token this may report red -- `skill up` below is
# the real end-to-end test.
agentos skill check

# Boot the runner with the model credential AND the GitHub token forwarded.
agentos skill up --secret GITHUB_PERSONAL_ACCESS_TOKEN

# Ask it something that exercises the authed server.
agentos skill message "List the open issues in curie-eng/agentos and group them by label."

agentos skill down
```

If the message reply cites real issue titles/numbers from the repo, the authed
MCP path worked end to end: token forwarded → server authenticated → tools
called → answer grounded in live data.

## Evals

`evals/cases.json` grades the agent the same way at every tier. With the runner
up (`skill up --secret ...`), run:

```bash
agentos skill eval
```

The three cases are deliberately robust to changing issue data — they assert on
things that do not churn: that the agent returns real issue numbers (`#\d+`),
that it finds the stable `enhancement` label when grouping, and that it can read
a specific **closed** historical issue (#7, whose title is about `aci-protocol`).
If the repo is ever restructured so those anchors no longer hold, update the
`expected` values here — a case that starts failing is the grader catching a real
change, which is the point.

## Swapping in a different service

The mechanism is service-agnostic. To point at another authed MCP server,
change `.mcp.json` (`command`/`args` for a stdio server, or `type`/`url`/
`headers` for a remote one) and forward its secret with `--secret <NAME>`. A
remote server that authenticates with a bearer token, for example, reads the
forwarded variable in its `headers` (`"Authorization": "Bearer ${TOKEN}"`).
