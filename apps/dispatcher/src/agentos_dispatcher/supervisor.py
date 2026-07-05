"""Reconnect supervision for a long-lived connection, with backoff and shutdown.

Socket Mode connections drop. The builtin Slack client self-heals transient
websocket drops on its own; this supervisor is the outer safety net for the
failures it cannot recover from (the connection factory raising on connect, an
unrecoverable client exit) and the owner of graceful shutdown.

The logic here is deliberately transport-agnostic: it drives a ``Connection``
(anything with ``run`` that blocks until the link is lost and ``close`` that
unblocks it), so it is unit-tested against a fake connection with an injected
sleep, no real socket required. The Socket Mode adapter that satisfies this
protocol lives in ``app.py``.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class Connection(Protocol):
    """A live connection the supervisor keeps up."""

    def run(self) -> None:
        """Establish and block until the connection is lost or ``close`` is called."""

    def close(self) -> None:
        """Tear the connection down and unblock ``run``."""


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff between reconnect attempts, capped at a maximum."""

    initial_seconds: float = 1.0
    max_seconds: float = 30.0
    multiplier: float = 2.0

    def delay(self, attempt: int) -> float:
        """Delay before reconnect ``attempt`` (0-based): initial * multiplier**attempt, capped."""
        raw = self.initial_seconds * (self.multiplier**attempt)
        return min(self.max_seconds, raw)


class Supervisor:
    """Keeps a Connection alive across drops until asked to stop.

    ``connect`` is a factory that returns a fresh Connection each attempt (a
    dropped connection is not reused). On any drop the supervisor sleeps for the
    backoff delay, then reconnects; the attempt counter grows monotonically so
    repeated rapid failures back off further, capped by the policy.
    """

    def __init__(
        self,
        connect: Callable[[], Connection],
        *,
        backoff: BackoffPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connect = connect
        self._backoff = backoff or BackoffPolicy()
        self._sleep = sleep if sleep is not None else _default_sleep
        self._logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._current: Connection | None = None
        self._lock = threading.Lock()

    def request_stop(self) -> None:
        """Ask the supervisor to shut down and unblock the current connection.

        Safe to call from a signal handler or another thread.
        """
        self._stop.set()
        with self._lock:
            current = self._current
        if current is not None:
            try:
                current.close()
            except Exception:  # pragma: no cover - close is best-effort on shutdown
                self._logger.exception("error closing connection during shutdown")

    def run(self) -> None:
        """Run the supervise loop until ``request_stop`` is called. Blocks."""
        attempt = 0
        while not self._stop.is_set():
            try:
                connection = self._connect()
                with self._lock:
                    self._current = connection
                connection.run()
            except Exception as exc:
                self._logger.warning("connection failed: %s", exc)
            finally:
                with self._lock:
                    self._current = None

            if self._stop.is_set():
                break

            delay = self._backoff.delay(attempt)
            attempt += 1
            self._logger.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
            self._sleep(delay)


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
