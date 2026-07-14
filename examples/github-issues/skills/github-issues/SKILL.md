---
name: github-issues
description: Read, summarize, and triage GitHub issues and pull requests for a repository using the authenticated GitHub MCP server. Invoke whenever the user asks about open issues, wants a repo's issues summarized or triaged, asks to find issues by label or author, or asks to draft an issue.
allowed-tools:
  - github
---

# GitHub issue triage

## When to run
The user asks about a repository's issues or pull requests — to list open
issues, summarize them, find ones by label/assignee/author, or draft a new
issue for review.

## How to answer
1. Identify the repository (`owner/repo`). Ask if the user did not name one.
2. Call the `github` server's tools:
   - list/search issues to pull the current open set,
   - fetch a single issue when the user asks about one in particular.
3. Summarize plainly: title, number, author, labels, and a one-line gist.
   Group by label or priority when triaging.
4. When asked to draft an issue, write the title and body and show it for the
   user to confirm — do not create it unless the user explicitly says to.

## Notes
`github` is the off-the-shelf `@modelcontextprotocol/server-github` stdio MCP
server, declared in this bundle's `.mcp.json`. It authenticates with a GitHub
personal access token read from `GITHUB_PERSONAL_ACCESS_TOKEN` in the sandbox
environment. That token is NOT in the bundle: you forward it at launch with
`agentos skill up --secret GITHUB_PERSONAL_ACCESS_TOKEN` (see the README), which
passes the variable by name into the container the same way model credentials
are forwarded. A read-scoped token is enough for listing and reading; creating
an issue needs `repo`/`issues` write scope.
