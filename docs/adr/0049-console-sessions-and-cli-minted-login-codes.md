# 49. Console sessions and CLI-minted login codes

Date: 2026-07-17

Status: Accepted

Implements [#630](https://github.com/curie-eng/agentos/issues/630).

## Context

The console authenticates to the API by sending the shared platform key from
browser JavaScript. `apiKey()` (`apps/ui/src/api/config.ts`) resolves it from a
`?api_key=` URL query parameter, else the build-time `VITE_API_KEY`, else the
published dev default `agentos-dev-key`, and `headers()`
(`apps/ui/src/api/client.ts`) attaches it as `X-API-Key` on every call.

None of those three inputs works on a sealed install, and the way they fail is
the problem. The chart generates a strong random `apiKey` per release
(`charts/agentos/templates/secrets.yaml`) and injects it into the API pod only
(`charts/agentos/templates/api.yaml`). The UI image never receives it: its
Dockerfile bakes no `VITE_API_KEY`, so `apiKey()` falls through to the hardcoded
dev default, which cannot match the release's random key. The console therefore
401s until the operator reads the key out of the Kubernetes Secret and appends
`&api_key=<real key>` to the URL that `agentos cluster status` printed.

That is the whole hole. The platform key is not a page credential: it authorizes
every router except the deliberately-scoped `state` router (ADR-0033) and the
GitHub webhook, which means deployments, approval resolution, memory, traces,
budgets, and the kill switch. Putting it in a URL to make the console usable
writes a credential with that blast radius into browser history, the Referer
header on any outbound link, proxy and ingress access logs, and shell history,
and ships it over plaintext NodePort traffic. The `?api_key=` parameter is not a
convenience that happens to be insecure; it is the only documented way to use a
sealed console, so every real operator is pushed onto it.

The launch scope is single operator, single tenant, self hosted. Tenant-scoped
principals are [#151](https://github.com/curie-eng/agentos/issues/151) and are
explicitly not in scope here. This decision must therefore close the credential
exposure without importing an identity system.

## Decision

The console authenticates with a **server-managed, revocable session cookie**,
established by exchanging a **single-use login code minted by the CLI**. The
browser never receives the platform key on any path.

**One session store.** A `console_sessions` table (Postgres, one Alembic
revision) holds a row per session: the SHA-256 of its login code, the SHA-256 of
its session token, both expiries, and `consumed_at` / `revoked_at`. Only hashes
are stored, so a database read cannot replay a session. Revocation is a column
write, which is what makes the session revocable in the sense the issue requires:
a durable row a human can kill, not a self-contained signed token that stays
valid until it expires.

**Minting is CLI-side and never handles the key by hand.** `agentos <local|cluster>
console login` calls `POST /console/login-codes` under the platform key and
prints a short-lived single-use code. On a cluster it sources the key through the
existing `discover_api_key()` (`cli/src/ops.rs`), which reads the release Secret
and flows the value straight into the `X-API-Key` header without ever printing
it. The operator copies a code, never the key.

**Exchange sets the cookie.** The console posts the code to `POST
/console/session`, an unauthenticated endpoint that consumes the code, mints a
session token, and returns it as a cookie: `HttpOnly` (browser JS cannot read
it), `Secure`, `SameSite=Strict`, `Path=/`. `HttpOnly` is what makes this
strictly stronger than the status quo: script on the page cannot exfiltrate the
credential it authenticates with.

**One shared dependency still gates every router.** `require_api_key`
(`apps/api/src/agentos_api/auth.py`) accepts the platform key **or** a live
console session, in that order, and stays the single dependency every router
depends on. The platform-key path is unchanged and hits no database, so the
worker, runner, and CLI are untouched. This extends the shared dependency rather
than adding a second auth scheme to a router, which is the boundary
`apps/api/CLAUDE.md` draws.

**A session cannot mint or manage sessions.** The three operator routes
(`POST /console/login-codes`, `GET /console/sessions`, `DELETE /console/sessions`)
depend on `require_platform_key`, which is the pre-existing platform-key-only
check: a strict subset of `require_api_key`, not a second scheme. This is
load-bearing rather than cautious. If a session cookie could mint a login code,
it could mint its own successor indefinitely, which is a refresh token by another
name and would quietly defeat the fixed absolute lifetime this ADR chose. Session
management is therefore reachable only from the CLI, which is where the platform
key already lives.

**TLS is enforced at exchange, fail-closed.** `POST /console/session` reads the
`Origin` header and refuses with a `{error, fix}` 400 unless the origin is
`https:` or a loopback host (`localhost`, `127.0.0.1`, `[::1]`), which browsers
treat as a secure context and for which they honor `Secure` cookies. A plaintext
NodePort origin is refused with the `kubectl port-forward` command as the fix.
The cookie is therefore only ever established over a channel that protects it,
and the failure is a legible instruction rather than a silently-unprotected
session.

**`SameSite=Strict` is the CSRF control**, backed by an `Origin` equality check
on the exchange. The API has no CORS middleware on purpose and the console is
strictly same-origin (`apps/ui/CLAUDE.md`), so a cross-site page can neither read
a response nor cause the cookie to ride along on a forged request.

`agentos cluster status` keeps printing the plain console URL with no secret in
it, and now names `agentos cluster console login` as the way to get in.

## Consequences

The raw platform key no longer has a browser-reachable path. `?api_key=`,
`VITE_API_KEY`, and the `agentos-dev-key` fallback are deleted from the UI rather
than deprecated, so there is no input left to regress onto; a build-output test
asserts the dev key string does not appear in `dist/`.

Logging into the console requires CLI access to the install. For a single
operator who already runs `agentos cluster up`, this is not a new dependency, and
it is strictly less handling than reading a Secret by hand. It would be the wrong
trade for a multi-user console, which is exactly what #151 exists to design.

A console session costs one indexed database read per request. The platform-key
path short-circuits before that read, so no machine caller pays it.

Sessions expire on a fixed absolute lifetime with no refresh. A long-lived
console tab will be asked to log in again. Sliding expiry is deliberately not
built: it is speculative until an operator complains.

## Alternatives considered

**A password form that posts the platform key.** The console shows a password
field, posts the key once, and gets a session cookie back. This is the
conventional shape and needs no CLI verb. Rejected: it still hands the raw
platform key to browser code. Script injected on the login page, a browser
extension, or a password manager sync would capture a credential that authorizes
deployments, approvals, and the kill switch platform-wide, and that credential
cannot be revoked without rotating the Secret and restarting the API. The
login-code exchange gives an attacker at that same moment nothing better than a
single revocable session. The issue's acceptance is explicit that browser code
must not receive the raw administrator credential, and a password field is
browser code receiving it.

**Injecting the key into the UI pod and proxying from nginx.** The UI pod already
proxies `/api`; it could hold the key and attach the header server-side.
Rejected: it authenticates the *pod*, not the operator, so anyone who can reach
the NodePort is an administrator. That is a worse hole than the one being closed,
and it is unrevocable.

**A signed stateless session token (JWT-shaped), reusing the ADR-0033 idiom.**
The `sandbox_token` HMAC pattern is proven in this codebase and needs no table.
Rejected: it is not revocable. A stolen token stays valid until expiry, and the
only kill switch is rotating `api_key`, which breaks the worker and runner at the
same time. The issue requires a revocable session, and revocation wants durable
state.

**An external identity provider or OAuth.** Rejected as out of scope: it imports
an identity system for one operator, and tenant-scoped principals are #151's
decision to make, not this one's to pre-empt.
