"""Liveness heartbeat: a file the event loop touches so a wedge is observable.

The worker's health is not "the process is alive" but "the asyncio event loop is
still turning". A pod whose loop is wedged (a blocking call, a deadlock) keeps its
process up and passes a naive TCP/process probe while doing no work. This task
runs on the same event loop as the consumers and touches a file each interval; a
wedged loop cannot run it, so the file's mtime goes stale and a k8s exec liveness
probe checking that staleness can restart the pod.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def run_heartbeat(path: str, interval_s: float, stop: asyncio.Event) -> None:
    """Touch ``path`` on entry and every ``interval_s`` seconds until ``stop`` is set.

    Mirrors the consumer's sleep-or-stop pattern so shutdown does not wait out a
    full interval: the wait wakes early when ``stop`` is set.

    A liveness helper must never crash the process it monitors, so a failed
    ``touch`` (transient /tmp issue, permissions, full disk) is logged and the
    loop continues; once the condition clears the file updates again.
    """
    while not stop.is_set():
        try:
            Path(path).touch()
        except OSError:
            logger.warning("heartbeat touch failed for %s", path, exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass
