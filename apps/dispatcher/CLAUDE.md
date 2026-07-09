# CLAUDE.md - apps/dispatcher

The Slack ingestion edge: Bolt for Python in Socket Mode. Full behavior spec
lives in `apps/dispatcher/README.md`; this file is the enforceable-rule
summary.

## Load-bearing invariants

- **Scope discipline: ack, dedupe, placeholder, enqueue -- nothing else.**
  Routing, the finish-race, steer/interrupt, and run orchestration belong to
  the worker (`apps/worker`). If you find yourself adding any
  decision about *how* a message gets answered, that decision belongs one
  layer up -- stop and move it, don't grow the dispatcher's scope.
- **The dispatcher owns the `QueuedSlackEvent` shape.** It is defined in
  `dispatcher.queue.QueuedSlackEvent` because the dispatcher is the producer.
  Do not move this model into `packages/` unilaterally -- if it needs to
  become a shared type, raise it in an issue/PR first rather than moving it
  from a dispatcher change.
- **Idempotency key is the Slack event id, not the message content.** Dedupe
  is `SET <dedupe_prefix><event_id> 1 NX EX <ttl>` in Valkey -- O(1), TTL-bounded,
  no scan. Order is always claim -> post placeholder -> `XADD`, so a
  duplicate delivery can never produce a second placeholder. Do not reorder
  these three steps.
- **The queue seam is one `payload` field, not a multi-field Stream entry.**
  Each Stream entry carries a single `payload` field holding the
  `QueuedSlackEvent` JSON. This lets fields be added to the payload without
  reshaping the Stream schema itself -- do not add a second top-level Stream
  field for a new piece of data; put it inside the JSON payload.
- **Reconnects are the supervisor's job, not the Slack client's.** The Bolt
  socket client self-heals transient drops; `dispatcher.supervisor.Supervisor`
  is the outer net for failures the client cannot recover from (connect
  failures, unrecoverable exits) and owns graceful shutdown (`request_stop`
  wired to SIGINT/SIGTERM). Do not add ad hoc retry logic elsewhere for a
  connection failure -- it belongs in the supervisor's `BackoffPolicy`.

## Config surface

`DispatcherConfig()` (a `pydantic_settings.BaseSettings`) reads `SLACK_APP_TOKEN`,
`SLACK_BOT_TOKEN`, `VALKEY_*`, `AGENTOS_STREAM` (must match the worker's
stream name), `AGENTOS_DEDUPE_PREFIX`/`_TTL_SECONDS`, `AGENTOS_PLACEHOLDER_TEXT`,
and the backoff tunables. Full table in `apps/dispatcher/README.md`.

## Verify (Slack-free)

```bash
docker compose -f compose.dev.yaml up -d valkey
uv run pytest apps/dispatcher/tests -q
```

Only the Slack Web API client and socket transport are faked. Stream and
dedupe assertions run against the real Valkey from `compose.dev.yaml`.
`tests/test_dispatch.py` drives Bolt's real `SocketModeHandler.handle` end to
end; `tests/test_queue.py` covers the seam and dedupe against real Valkey;
`tests/test_supervisor.py` covers backoff/reconnect/shutdown with a fake
connection. A new test that fakes Valkey instead of using the real one from
the compose stack does not meet this repo's testing bar (root AGENTS.md:
never mock Postgres/Valkey/Langfuse).
