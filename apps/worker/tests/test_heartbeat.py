"""Heartbeat liveness for the worker (issue #71).

``run_heartbeat`` touches a file on the worker's own asyncio loop so a liveness
probe can detect a wedged event loop: if the loop stalls, the mtime stops
advancing. These tests drive the real coroutine against a real file (tmp_path)
with a tiny interval, matching the repo's asyncio.run test style
(apps/worker/tests/kernel/test_consumer.py).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from agentos_worker.config import WorkerConfig
from agentos_worker.heartbeat import run_heartbeat


def test_run_heartbeat_touches_file_and_exits_promptly_on_stop(tmp_path) -> None:
    path = tmp_path / "worker.heartbeat"

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_heartbeat(str(path), 0.01, stop))
        try:
            # The first touch happens immediately on start.
            deadline = time.monotonic() + 1.0
            while not path.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert path.exists()
            # The mtime is fresh, not a stale file left from a prior run.
            assert abs(path.stat().st_mtime - time.time()) < 1.0

            # Setting stop makes it exit well within one interval, not a full one.
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)
        finally:
            stop.set()
            task.cancel()

    asyncio.run(go())


def test_run_heartbeat_touches_repeatedly(tmp_path) -> None:
    path = tmp_path / "worker.heartbeat"

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_heartbeat(str(path), 0.02, stop))
        try:
            deadline = time.monotonic() + 1.0
            while not path.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert path.exists()

            first = path.stat().st_mtime_ns
            # Sleep across several intervals so a subsequent touch must land.
            await asyncio.sleep(0.1)
            second = path.stat().st_mtime_ns
            assert second > first
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(go())


def test_run_heartbeat_survives_touch_failure_and_exits_on_stop() -> None:
    # Parent directory does not exist, so touch() raises FileNotFoundError.
    # The loop must swallow it (never propagate) and still exit on stop.
    path = "/nonexistent-dir-xyz/hb"

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_heartbeat(path, 0.01, stop))
        try:
            # Let the loop turn a few times through the failing touch.
            await asyncio.sleep(0.05)
            assert not task.done()  # the guard swallowed the error, loop alive
            stop.set()
            # Exits promptly despite every touch failing.
            await asyncio.wait_for(task, timeout=1.0)
        finally:
            stop.set()
            task.cancel()

    asyncio.run(go())


def test_worker_config_heartbeat_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_HEARTBEAT_FILE", raising=False)
    monkeypatch.delenv("AGENTOS_HEARTBEAT_INTERVAL_SECONDS", raising=False)

    cfg = WorkerConfig()

    assert cfg.heartbeat_file == "/tmp/agentos-worker.heartbeat"
    assert cfg.heartbeat_interval_s == 10.0


def test_worker_config_reads_heartbeat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_HEARTBEAT_FILE", "/var/run/agentos/wk.hb")
    monkeypatch.setenv("AGENTOS_HEARTBEAT_INTERVAL_SECONDS", "2.5")

    cfg = WorkerConfig()

    assert cfg.heartbeat_file == "/var/run/agentos/wk.hb"
    assert cfg.heartbeat_interval_s == 2.5
