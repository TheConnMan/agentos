# cli

Owning task: **I1**. The `agentos` CLI (Rust: clap + tokio + reqwest): `init`, `start`, `send`, `eval`. It speaks only the frozen contracts over HTTP/NDJSON and orchestrates a local runner container via Docker, so `agentos send "..."` round-trips a synthetic event through a local runner and streams the reply with zero Slack involved. Off the critical path, so the slower Rust iteration loop costs nothing. R0 ships only a compiling skeleton (binary `agentos`, one trivial test) so `cargo test` is green; I1 builds the real subcommands.

Verify: `cd cli && cargo fmt --check && cargo clippy -- -D warnings && cargo test`.
