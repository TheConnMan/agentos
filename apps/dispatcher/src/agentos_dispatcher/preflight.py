"""Boot-time gate on the platform API wiring (#442, AC2).

The dispatcher resolves Slack approval clicks by calling the platform API. When
``AGENTOS_API_URL`` points somewhere unreachable, the only symptom today is
a warning at click time and a dead-ended button: ``ApprovalResolveClient.resolve``
catches the ``httpx.HTTPError`` and returns ``ResolveOutcome(status_code=0)``.
This gate turns that silent misconfiguration into a loud boot failure naming the
URL it could not reach.

The gate is bounded retry, not a single probe: one probe at t=0 races the API's
own startup, and in Kubernetes pod start order is not ordered at all, so
fail-immediately would crash-loop a healthy stack. Restart backoff is the outer
retry loop there, and a CrashLoopBackOff with the named URL in the log is the
operator signal.

It is a wiring gate, not a liveness monitor: one shot at boot only. The heartbeat
probes own liveness and ``ApprovalResolveClient`` owns per-call degradation, so an
API restart hours later must not kill the dispatcher.

``/health`` is unauthenticated by design, so this gate proves reachability only: a
wrong ``AGENTOS_API_KEY`` still passes it and surfaces as a 401 at the first
approval click.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import DispatcherConfig
from .supervisor import BackoffPolicy

# Cap on any single probe, so one hung connect cannot eat the whole deadline and
# collapse bounded retry into the single probe the design rejected. A black-holed
# address (a DROP'd rule, an unroutable IP) hangs until this fires rather than
# refusing fast like a closed port does.
_MAX_PROBE_TIMEOUT_S = 5.0


class ApiUnreachableError(RuntimeError):
    """The platform API did not answer ``GET /health`` before the deadline."""


def _safe_for_log(url: str) -> str:
    """Drop any userinfo from a URL so credentials cannot reach the logs.

    AC2 requires naming the resolved URL, so the URL itself must survive; only
    the ``user:pass@`` part is removed. ``httpx`` accepts userinfo, so a BYO
    ``dispatcher.apiBaseUrl`` could otherwise write it into pod logs and any log
    shipper downstream.
    """
    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def check_api_reachable(
    config: DispatcherConfig,
    *,
    logger: logging.Logger,
    client: httpx.Client | None = None,
) -> None:
    """Poll ``GET {api_base_url}/health`` until it answers 200 or the deadline passes.

    Raises ``ApiUnreachableError`` naming the resolved base URL when the deadline
    expires.
    """
    # The resolve path rstrips its base too; without this a perfectly reasonable
    # "http://agentos-api:8000/" would probe "//health" and fail a wired stack.
    base = config.api_base_url.rstrip("/")
    logged_base = _safe_for_log(base)
    timeout_s = config.api_preflight_timeout_s

    backoff = BackoffPolicy(
        initial_seconds=config.backoff_initial_seconds,
        max_seconds=config.backoff_max_seconds,
        multiplier=config.backoff_multiplier,
    )
    http = client or httpx.Client(timeout=min(_MAX_PROBE_TIMEOUT_S, timeout_s))
    owned = client is None
    start = time.monotonic()
    deadline = start + timeout_s
    attempt = 0
    last_error = ""
    try:
        while True:
            # Bound each probe to what is left of the budget, so the loop cannot
            # start a request it has no time for and then block past the deadline
            # on that request's own timeout. Without this a 30s deadline takes
            # ~35s and a short one nearly doubles.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = http.get(
                    f"{base}/health", timeout=min(_MAX_PROBE_TIMEOUT_S, remaining)
                )
                if response.status_code == 200:
                    logger.info("platform API reachable at %s", logged_base)
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)

            delay = backoff.delay(attempt)
            attempt += 1
            # Clamp to the remaining budget rather than breaking when the next
            # delay would overshoot it: with the shipped defaults (backoff_max
            # 30.0 vs a 30.0s deadline) breaking abandons half the budget, and
            # raising the deadline then buys the operator nothing.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
    finally:
        if owned:
            http.close()

    elapsed = time.monotonic() - start
    raise ApiUnreachableError(
        f"cannot reach the platform API at {logged_base} after {elapsed:.1f}s "
        f"({attempt} attempts, last error: {last_error}); check AGENTOS_API_URL"
    )
