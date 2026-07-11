"""A live ``ModelSession`` backed by an ``opencode serve`` subprocess.

This is the real (non-scripted) OpenCode harness for issue #25. It implements
the runner's ``ModelSession`` protocol (``connect`` / ``query`` /
``receive_turn`` / ``interrupt`` / ``close``) by supervising a local
``opencode serve`` process, driving it over its HTTP API, and turning its live
Server-Sent-Event stream into claude-agent-sdk messages via
:class:`~agentos_runner.opencode.synth.TurnSynthesizer`. Because the shim emits
SDK-shaped messages, the entire runner core runs unmodified (see ``synth.py``).

Wire protocol (OpenCode v1, verified against ``opencode`` v1.17.17 ``GET /doc``):

- ``connect``   -> ``POST /session`` creates a session; a background task opens
  the global ``GET /event`` SSE bus and buffers this session's frames.
- ``query``     -> ``POST /session/{id}/prompt_async`` (returns 204; output
  streams on the event bus).
- ``receive_turn`` drains the buffered frames through a fresh ``TurnSynthesizer``
  until the turn's terminal result.
- ``interrupt`` -> ``POST /session/{id}/abort``.
- ``close``     tears down the SSE reader, the HTTP session, and the subprocess.

Two spike semantic gaps (documented, not solved here):

* **Steer is completion-deferred.** OpenCode admits a message posted mid-turn at
  the next turn boundary rather than interleaving it into the live generation, so
  a steer lands as the next turn's prompt. The runner's steer path still works;
  the timing differs from the claude-agent-sdk's first-class mid-turn steer.
* **No resume equivalent.** OpenCode has no ``resume`` handle matching the SDK's,
  so ``AGENTOS_HISTORY_REF`` rehydration is unsupported and every session
  cold-starts. History replay is out of scope for this spike.

Concurrency note: this uses ``asyncio`` primitives directly (queue, task, event)
and therefore assumes the runner's ``anyio`` host is on its default asyncio
backend, which is what ``anyio.run`` and the runner's HTTP server use. aiohttp
mandates asyncio regardless.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .synth import TurnSynthesizer, _result

# OpenCode's lowercase read-only built-ins, declared here as this harness's
# read-only set for the deny-by-default side-effect classifier (side_effects.py).
# The tool surface was enumerated live against opencode 1.17.17 (GET
# /experimental/tool): bash, read, glob, grep, edit, write, task, webfetch,
# todowrite, skill, plus the question/invalid pseudo-tools. Of those, only read,
# grep, glob, webfetch, and skill are pure reads (skill loads a skill's
# instructions into the conversation, an idempotent read). bash/edit/write/task/
# todowrite are deliberately NOT listed, and question is excluded as interactive
# rather than a pure read. Deny-by-default covers anything uncertain or unknown.
OPENCODE_READONLY_TOOLS: frozenset[str] = frozenset(
    {"read", "grep", "glob", "webfetch", "skill"}
)

# Default cheap real model routed through OpenRouter. GLM-4.6 reliably returns a
# concrete short text answer (unlike GLM-5.2, which can return reasoning + empty
# text on trivial prompts, issue #107). Override via env for a different model.
DEFAULT_PROVIDER = os.environ.get("OPENCODE_SPIKE_PROVIDER", "openrouter")
DEFAULT_MODEL = os.environ.get("OPENCODE_SPIKE_MODEL", "z-ai/glm-4.6")

# Max seconds of silence on the event bus before a turn is declared timed out.
# The ACI stream must always terminate; a wedged provider call otherwise hangs
# ``receive_turn`` forever. Generous so a slow-but-live model is not cut off.
_SILENCE_TIMEOUT = float(os.environ.get("OPENCODE_SPIKE_SILENCE_TIMEOUT", "120"))

# How long to wait for ``opencode serve`` to answer /api/health after launch.
_READY_ATTEMPTS = 160
_READY_INTERVAL = 0.25


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _find_opencode() -> str:
    """Locate the opencode binary (PATH, then the standard install location)."""

    found = shutil.which("opencode")
    if found:
        return found
    fallback = os.path.expanduser("~/.opencode/bin/opencode")
    if os.path.exists(fallback):
        return fallback
    raise RuntimeError("opencode binary not found on PATH or ~/.opencode/bin")


def _subprocess_env() -> dict[str, str]:
    """Build the subprocess env, forwarding exactly the OpenRouter credential.

    OpenCode expects ``OPENROUTER_API_KEY``; this project stores it as
    ``OPENROUTER_TOKEN``. The key value is never logged or echoed.
    """

    env = dict(os.environ)
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_TOKEN")
    if key:
        env["OPENROUTER_API_KEY"] = key
    # Ensure the bun-shipped opencode runtime is resolvable.
    extra = os.pathsep.join(
        p for p in (os.path.expanduser("~/.opencode/bin"), os.path.expanduser("~/.bun/bin")) if p
    )
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    return env


class OpenCodeModelSession:
    """ModelSession driving one live ``opencode serve`` process over its HTTP API."""

    def __init__(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self._model = model or DEFAULT_MODEL
        self._provider = provider or DEFAULT_PROVIDER
        self._cwd = cwd
        self._bin = _find_opencode()

        self._proc: subprocess.Popen[bytes] | None = None
        self._base: str | None = None
        self._sid: str | None = None
        self._http: aiohttp.ClientSession | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._workdir: str | None = None
        self._owns_cwd = False
        self._log: Any = None

    async def connect(self) -> None:
        workdir = self._cwd
        if workdir is None:
            workdir = tempfile.mkdtemp(prefix="agentos-opencode-")
            self._owns_cwd = True
        self._workdir = workdir
        port = _free_port()
        self._base = f"http://127.0.0.1:{port}"
        self._log = tempfile.NamedTemporaryFile(
            prefix="opencode-serve-", suffix=".log", delete=False
        )
        self._proc = subprocess.Popen(
            [self._bin, "serve", "--port", str(port), "--hostname", "127.0.0.1",
             "--log-level", "ERROR"],
            stdout=self._log,
            stderr=self._log,
            env=_subprocess_env(),
            cwd=workdir,
        )
        self._http = aiohttp.ClientSession()
        await self._await_ready()
        async with self._http.post(f"{self._base}/session", json={}) as resp:
            resp.raise_for_status()
            self._sid = str((await resp.json())["id"])
        self._reader_task = asyncio.create_task(self._read_events())
        # Ensure the SSE stream is established before any prompt is posted, so no
        # turn frame is emitted before the reader is listening.
        await asyncio.wait_for(self._connected.wait(), timeout=15)

    async def _await_ready(self) -> None:
        assert self._http is not None and self._base is not None
        for _ in range(_READY_ATTEMPTS):
            try:
                async with self._http.get(
                    f"{self._base}/api/health", timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception:  # noqa: BLE001 - a starting server refuses/times out; retry
                pass
            await asyncio.sleep(_READY_INTERVAL)
        raise RuntimeError("opencode serve did not become ready")

    async def _read_events(self) -> None:
        assert self._http is not None and self._base is not None
        try:
            async with self._http.get(
                f"{self._base}/event",
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
            ) as resp:
                self._connected.set()
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        frame = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    props = frame.get("properties", {})
                    sid = props.get("sessionID") if isinstance(props, dict) else None
                    # Keep only this session's frames; global bus noise
                    # (server.connected, plugin.added, ...) carries no sessionID.
                    if sid == self._sid:
                        await self._queue.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dropped stream must still let
            # an in-flight receive_turn terminate rather than hang on an empty queue.
            self._connected.set()
            await self._queue.put(
                {
                    "type": "session.error",
                    "properties": {
                        "sessionID": self._sid,
                        "error": {"message": f"event stream closed: {exc}"},
                    },
                }
            )

    async def query(self, text: str) -> None:
        if self._http is None or self._base is None or self._sid is None:
            raise RuntimeError("session not connected")
        body = {
            "model": {"providerID": self._provider, "modelID": self._model},
            "parts": [{"type": "text", "text": text}],
        }
        async with self._http.post(
            f"{self._base}/session/{self._sid}/prompt_async", json=body
        ) as resp:
            if resp.status not in (200, 204):
                detail = await resp.text()
                raise RuntimeError(f"opencode prompt_async failed: {resp.status} {detail}")

    async def receive_turn(self) -> AsyncIterator[Any]:
        synth = TurnSynthesizer()
        start = time.monotonic()
        last_type = "none"
        while not synth.done:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=_SILENCE_TIMEOUT)
            except TimeoutError:
                elapsed = time.monotonic() - start
                yield _result(
                    text=(
                        f"opencode turn produced no output for {_SILENCE_TIMEOUT:.0f}s "
                        f"(last frame '{last_type}', {elapsed:.0f}s into turn)"
                    ),
                    is_error=True,
                    usage=None,
                )
                return
            last_type = str(frame.get("type", "unknown"))
            for message in synth.ingest(frame):
                yield message

    async def interrupt(self) -> None:
        if self._http is None or self._base is None or self._sid is None:
            return
        try:
            async with self._http.post(
                f"{self._base}/session/{self._sid}/abort",
                timeout=aiohttp.ClientTimeout(total=10),
            ):
                pass
        except Exception:  # noqa: BLE001 - a best-effort abort must not raise into
            # the runner's interrupt/finally path (which itself suppresses).
            pass

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._log is not None:
            try:
                self._log.close()
            except Exception:  # noqa: BLE001
                pass
            self._log = None
        if self._owns_cwd and self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
