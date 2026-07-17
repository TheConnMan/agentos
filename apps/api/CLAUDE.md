# CLAUDE.md - apps/api

FastAPI server: agents/versions/deployments CRUD, auth, the plugin bundle
pipeline, the GitHub git-flow engine, and the Langfuse/pod-log observability
proxies. See `../../ARCHITECTURE.md` for how this service fits between the
worker, Postgres, MinIO/S3, Langfuse, and GitHub.

## Load-bearing invariants

- **Auth is one shared dependency, carrying two credentials today.**
  `require_api_key` (`auth.py`) accepts EITHER the shared platform key (the
  `X-API-Key` header, compared against `Settings.api_key` with
  `hmac.compare_digest`) OR a live console session (ADR-0049, #630). The
  platform key is explicitly MVP-only; GitHub-App identity work is expected to
  replace it eventually, but until that lands, do not add a second auth
  scheme for a new router without raising it in an issue/PR first -- every
  router should share the one dependency. Order inside it is load-bearing: the
  platform-key check runs first and returns before the session store is read, so
  machine callers pay no database read.
- **The console session is a browser credential and needs the CSRF header
  (ADR-0049, #630).** `require_api_key` accepts the `agentos_console_session`
  cookie only when the request also carries `X-Console-Session`
  (`CONSOLE_SESSION_HEADER`). A cookie is ambient authority and `SameSite=Strict`
  does not close that -- "site" ignores the port, so `http://localhost:8080` is
  same-site with every other localhost port, and the body-less `POST
  /agents/{id}/kill` is a CORS-simple request nothing preflights. The custom
  header cannot be set cross-origin without a preflight this CORS-free API
  fails. Do not drop the header requirement, and do not extend it to the
  platform-key path: it is a browser-only defense, and the CLI/worker/runner must
  keep authenticating with the header credential alone.
- **`require_platform_key` is the platform-key-only subset**, used by the console
  operator routes (`POST /console/login-codes`, `GET|DELETE /console/sessions`)
  so a session cannot mint its own successor or mass-revoke the store it belongs
  to (ADR-0049). It is a strict subset of `require_api_key`, not a second scheme.
  Reach for it when a route must be the CLI's and not the browser's.
- **The `state` router authenticates differently, on purpose (ADR-0033, #410).**
  `require_state_access` (`routers/state.py`) accepts EITHER the platform key OR a
  scoped, path-`agent_id`-bound `state` token minted by the worker for the
  sandbox, so a sandboxed agent can rehydrate its own memory/transcript without
  holding a resolve-capable platform-wide key. It does NOT accept a console
  session; equally, a scoped `state` token is rejected by `require_api_key`
  everywhere else, including `/approvals/{id}/resolve`. The two dependencies are
  deliberately disjoint extensions of the platform key, so neither is a
  laundering path into the other. Do not collapse the state router back onto
  `require_api_key`, and do not extend scoped-token acceptance to another router
  without a new ADR.
- **The GitHub webhook is authenticated differently, on purpose.** `/github/webhook`
  verifies the HMAC signature GitHub sends (`x-hub-signature-256` against
  `settings.github_webhook_secret`), not the API key -- GitHub cannot send an
  `X-API-Key` header. It lives outside the `require_api_key` dependency
  deliberately (`routers/github.py`); do not add the API-key dependency to it.
- **Git-flow never calls the GitHub API.** `gitflow.py` builds the bundle by
  archiving the pushed sha directly from the repo over the git protocol (bare
  repos in tests, the real remote in production). This keeps the flow
  independent of GitHub API rate limits and scopes. Do not introduce a GitHub
  API client into `gitflow.py` for something the git protocol already gives
  you.
- **Prod push reuses the dev-built bundle; it does not rebuild.** A push to
  the prod branch looks up the `Version` already created for that sha (from
  the dev push) and only creates a new `Deployment` row. If you find yourself
  rebuilding on promote, that is a bug, not a feature -- promotion is meant to
  be "the exact artifact that passed on dev," not a fresh build.
- **The plugin bundle validator (`plugin_format.validate_bundle`) is the only
  gate a bundle passes through**, whether it arrives via the CLI's
  `agentos local deploy` / `agentos cluster deploy`, the UI's create-agent
  modal, or a git push. Do not
  duplicate validation logic in a new entry point; route through
  `bundles.py`.
- **Observability endpoints are read-only proxies, not new stores.** The
  `/observability/metrics/*` endpoints compute aggregates from Langfuse's
  public API (`metrics.py` + `langfuse.py`); the runner-pod-log endpoint
  proxies the K8s pod-logs API (`k8s.py`) for the sandbox that served a given
  trace. Neither should grow a local cache or its own persistence -- Langfuse
  and the cluster are the source of truth.
- **The `/observability/runners/.../logs` endpoint has three distinct error
  states by design**: 503 when no kubeconfig is configured, 404 when the pod
  is gone, 502 for any other cluster error. The UI renders each differently
  (`apps/ui/CLAUDE.md`); do not collapse these into a single error shape.

## Migrations: never develop against the shared compose DB

**Alembic migrations must be developed and tested against a disposable
database (or schema), never the shared `compose.dev.yaml` Postgres.** That
Postgres is shared state every other lane's test suite reads on setup;
stamping it with an unmerged revision breaks everyone else's `alembic upgrade
head` (`Can't locate revision ...`) until your branch merges or you unstamp
it. This has already bitten concurrent work in this repo more than once. Spin
up a scratch Postgres (a second compose service, a throwaway container, or a
fresh schema) for migration development; only run migrations against the shared
compose DB once your revision is merged to main.

## Config surface

`Settings` (`config.py`, env-driven) covers the Postgres DSN, the bundle
store (MinIO/S3 endpoint + bucket), the Langfuse public API base + keys, the
GitHub webhook secret, `api_key`, `kube_config_path` (empty = no cluster
configured, the 503 case above), and `metrics_default_window_hours`.

## Verify

```bash
cd apps/api && uv run alembic upgrade head   # apply schema once against compose.dev.yaml
uv run pytest apps/api/tests -q               # from repo root; needs the dev stack up
uv run python -m agentos_api.export_openapi   # regenerate committed openapi.json; check for drift
```

`test_openapi_drift.py` fails if the committed OpenAPI spec is stale --
regenerate it after any router signature change, don't hand-edit the JSON.
Integration tests (`test_gitflow_integration.py`, `test_langfuse_integration.py`,
`test_metrics_integration.py`) run against the real Postgres/MinIO from
`compose.dev.yaml`; only Langfuse's own API responses and the GitHub webhook
sender are faked.
