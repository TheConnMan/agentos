# apps/dispatcher

The Slack dispatcher: Slack Bolt for Python in Socket Mode.
On an `app_mention` (channel) or direct `message` (DM) for the bot it acks the
Socket Mode envelope fast, posts an in-thread placeholder reply, and enqueues a
normalized job onto a Valkey Stream keyed by the Slack event id (idempotent),
under reconnect supervision with graceful shutdown.

It does exactly that and no more: routing, the finish-race, steer/interrupt, and
run orchestration are the worker's job, not the dispatcher's.

## The queue seam (what the worker consumes)

The dispatcher `XADD`s onto a Valkey Stream (`AGENTOS_STREAM`, default
`agentos:runs`). Each entry carries one field, `payload`, holding the JSON of a
`QueuedSlackEvent` (`dispatcher.queue.QueuedSlackEvent`):

| field | meaning |
|---|---|
| `slack_event_id` | Slack's per-delivery event id; the idempotency key |
| `thread_ts` | canonical thread key (root message ts of the thread) |
| `channel` | Slack channel id |
| `user` | Slack user id that authored the message |
| `text` | message text |
| `placeholder_ts` | ts of the placeholder reply already posted; the worker edits this message in place with the real response |
| `received_at` | ISO-8601 UTC timestamp the dispatcher received it |

The worker reconstructs it with `QueuedSlackEvent.from_stream_fields(fields)` (import from
`agentos_dispatcher`, or mirror the model). The model is defined inside the
dispatcher because the dispatcher is the producer and owns the shape; if it later
deserves promotion into a shared package, the maintainers make that call (the
dispatcher does not touch `packages/`). The single-`payload`-field encoding keeps
the seam explicit and lets fields be added without reshaping the Stream schema.

## Dedupe (idempotency)

Idempotency key = Slack event id (detailed-architecture 2b rule 5). A retried
Slack delivery must not enqueue twice. Before posting or enqueuing, the dispatcher
claims the event with a Valkey `SET <dedupe_prefix><event_id> 1 NX EX <ttl>`; the
first delivery wins and proceeds, a retry finds the key set and is dropped (still
acked, never re-posted, never re-enqueued). Chosen over stream-side dedupe because
it is O(1), TTL-bounded (no unbounded dedupe set to prune), and needs no Stream
scan. Order is claim -> post placeholder -> `XADD`, so a duplicate never produces
a second placeholder.

## Reconnect supervision and shutdown

`dispatcher.supervisor.Supervisor` drives a transport-agnostic `Connection`
(anything that blocks in `run` until the link drops and unblocks on `close`). The
builtin Slack client self-heals transient websocket drops; the supervisor is the
outer net for failures it cannot recover (the connection factory raising on
connect, an unrecoverable exit) and the owner of graceful shutdown. On a drop it
sleeps for an exponential, capped backoff (`BackoffPolicy`) and reconnects with a
fresh connection; `request_stop` (wired to SIGINT/SIGTERM) closes the current
connection and exits the loop without reconnecting. The Socket Mode adapter
(`app.SocketModeConnection`) is the thin production `Connection`.

## Config surface (env vars)

Parsed by `DispatcherConfig.from_env(os.environ)`.

| env var | default | meaning |
|---|---|---|
| `SLACK_APP_TOKEN` | "" | app-level token (`xapp-...`), Socket Mode |
| `SLACK_BOT_TOKEN` | "" | bot token (`xoxb-...`), Web API |
| `SLACK_SIGNING_SECRET` | "" | optional; unused in Socket Mode, kept for Bolt App construction |
| `VALKEY_HOST` | `localhost` | Valkey host (in-cluster: `valkey`) |
| `VALKEY_PORT` | `6379` | Valkey port (compose maps it to `56379` on the host) |
| `VALKEY_PASSWORD` | "" | Valkey password (compose dev: `valkeypass`) |
| `VALKEY_DB` | `0` | Valkey db index |
| `AGENTOS_STREAM` | `agentos:runs` | Stream the jobs land on |
| `AGENTOS_DEDUPE_PREFIX` | `agentos:dedupe:` | dedupe key prefix |
| `AGENTOS_DEDUPE_TTL_SECONDS` | `3600` | dedupe guard TTL |
| `AGENTOS_PLACEHOLDER_TEXT` | `On it. Working on your request.` | placeholder reply text |
| `AGENTOS_BACKOFF_INITIAL_SECONDS` | `1.0` | first reconnect backoff |
| `AGENTOS_BACKOFF_MAX_SECONDS` | `30.0` | backoff cap |
| `AGENTOS_BACKOFF_MULTIPLIER` | `2.0` | backoff growth factor |

The two Slack tokens are the only secrets. When a workspace exists they come from
the app's install (App-Level Token with `connections:write` for `SLACK_APP_TOKEN`;
Bot User OAuth Token for `SLACK_BOT_TOKEN`), delivered as env vars (a K8s Secret
in the chart). Nothing else is secret.

## Run it

```bash
python -m agentos_dispatcher
```

## Runbook: point it at a real Slack workspace (once one exists)

1. Create a Slack app. Fastest path: at <https://api.slack.com/apps> choose
   "From a manifest" and paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml),
   which already sets Socket Mode on, the bot scopes (`app_mentions:read`,
   `chat:write`, `im:history`, `im:read`), and the `app_mention` + `message.im`
   event subscriptions. (To do it by hand, configure exactly those.)
2. Generate an App-Level Token with `connections:write` -> `SLACK_APP_TOKEN`
   (`xapp-...`); copy the Bot User OAuth Token -> `SLACK_BOT_TOKEN` (`xoxb-...`).
3. Set both env vars (plus `VALKEY_*` for the target Valkey) and run
   `python -m agentos_dispatcher`. @-mention the bot in a channel it is in, or DM
   it: you should see the placeholder reply appear and one entry land on the
   Stream (`XLEN agentos:runs`). The worker consumes from there.

## Verification (Slack-free)

All tests run without a Slack workspace: the Slack Web API client and socket
transport are the only things faked; Stream and dedupe assertions run against the
real Valkey from `compose.dev.yaml` (host port `56379`). From the repo root:

```bash
docker compose -f compose.dev.yaml up -d valkey
uv run pytest apps/dispatcher/tests -q
```

`tests/test_dispatch.py` drives Bolt's real `SocketModeHandler.handle` end to end
(envelope -> ack -> placeholder -> `XADD`), including the duplicate-delivery and
event-filtering cases; `tests/test_queue.py` covers the seam and dedupe against
real Valkey; `tests/test_supervisor.py` covers backoff, reconnect, and graceful
shutdown with a fake connection.
