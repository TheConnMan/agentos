"""Runner entrypoint: build the session from the environment and serve the ACI.

Reads the ACI ``AGENTOS_*`` / ``OTEL_EXPORTER_OTLP_*`` env into a RunnerConfig,
wires the real claude-agent-sdk session (validated plugin bundle, budget, OTel),
and serves the HTTP channel. The session is started in ``on_startup`` so a plugin
or connect failure fails the process visibly rather than after the port is up.
"""

from __future__ import annotations

import logging
import os
import sys

import anyio
from aiohttp import web

from .adapter import ClaudeAgentSession, ModelSession, build_options
from .approval import build_approval_server
from .config import RunnerConfig
from .fake import FakeModelSession
from .history import (
    TranscriptStore,
    TurnRecord,
    format_conversation_preamble,
    resolve_history,
)
from .hooks import load_bundle_hooks
from .memory import MemoryRecord, MemoryStore, format_memory_preamble, resolve_memory
from .otel import RunTracer, build_tracer_provider
from .plugin import load_bundle_system_prompt, load_plugins
from .sdk_auth import UnsupportedCredentialError, resolve_sdk_env
from .server import create_app
from .session import SessionRunner
from .side_effects import SideEffectClassifier

logger = logging.getLogger(__name__)


def _compose_system_prompt(
    base: str | None,
    memory_preamble: str | None,
    conversation_preamble: str | None = None,
) -> str | None:
    """Prepend the loaded-memory and conversation preambles to the system prompt.

    State delivered from outside the sandbox becomes durable model context by
    leading the system prompt: durable memory (ADR-0025) first, then this thread's
    recovered conversation (ADR-0029), then the bundle/env system prompt. Any part
    may be absent.
    """

    parts = [p for p in (memory_preamble, conversation_preamble, base) if p]
    return "\n\n".join(parts) if parts else None


def build_runner(
    config: RunnerConfig,
    *,
    fake_model: bool = False,
    sdk_env: dict[str, str] | None = None,
    memory_store: MemoryStore | None = None,
    memory_preamble: str | None = None,
    history_store: TranscriptStore | None = None,
    conversation_preamble: str | None = None,
) -> SessionRunner:
    """Wire a SessionRunner backed by a real claude-agent-sdk session.

    ``fake_model`` (env ``AGENTOS_FAKE_MODEL``) swaps in the scripted fake session
    so the image can round-trip a synthetic event with no model credential or
    network -- used for the container smoke and any offline exercise of the wiring
    (OTel export included). It never reaches the Anthropic API.
    """

    # The effective system prompt: the ``AGENTOS_SYSTEM_PROMPT`` env value wins
    # for backward compatibility; otherwise fall back to the ``systemPrompt``
    # shipped in the bundle manifest (versioned with the agent, epic #30).
    system_prompt = config.system_prompt
    if system_prompt is None:
        system_prompt = load_bundle_system_prompt(config.session.plugin_dir)
    # Prior memory (#264) and this thread's recovered conversation (#20), both
    # loaded from outside the sandbox, lead the system prompt so the model sees
    # learned lessons and the prior exchange as durable context.
    system_prompt = _compose_system_prompt(system_prompt, memory_preamble, conversation_preamble)
    # In-bundle PreToolUse guardrails declared in the manifest hooks field (#272),
    # translated into SDK HookMatcher callbacks. None when the bundle declares none.
    bundle_hooks = load_bundle_hooks(config.session.plugin_dir)

    def factory() -> ModelSession:
        if fake_model:
            return FakeModelSession()
        plugins = load_plugins(config.session.plugin_dir)
        options = build_options(
            plugins=plugins,
            model=config.model,
            system_prompt=system_prompt,
            max_turns=config.max_turns,
            max_budget_usd=config.max_usd_per_day,
            # History is rehydrated harness-agnostically as a conversation preamble
            # (ADR-0029), not through the SDK-specific resume path, so history_ref
            # no longer feeds resume. build_options keeps the param for an explicit
            # caller; the boot path passes None.
            resume=None,
            task_budget_hint=config.session.budget.task_budget_hint,
            env=sdk_env or {},
            hooks=bundle_hooks,
            # Every session carries the in-process approval-request tool, so a
            # skill can raise a policy gate (ADR-0010) without the bundle
            # shipping its own MCP server for it.
            mcp_servers={"agentos": build_approval_server()},
        )
        return ClaudeAgentSession(options)

    provider = build_tracer_provider(
        config.session.otel,
        config.session.session_id,
        config.session.sandbox_id,
    )
    return SessionRunner(
        session_factory=factory,
        ceiling=config.ceiling,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(config.idempotent_tools),
        trace_name=f"agentos-run:{config.session.session_id}",
        session_id=config.session.session_id,
        model=config.model,
        memory_store=memory_store,
        history_store=history_store,
    )


def _load_memory(config: RunnerConfig) -> tuple[MemoryStore, str | None]:
    """Resolve AGENTOS_MEMORY_REF and load prior memory into a boot preamble.

    Runs synchronously at boot (before the port is up), so a bad ref or an
    unreachable store fails the process visibly rather than after serving. A
    transient load failure degrades to "no memory" and does NOT block boot -- an
    agent must still be able to run when its memory store is briefly unavailable.
    """

    store = resolve_memory(config.session.memory_ref, os.environ)

    async def _load() -> list[MemoryRecord]:
        return await store.load()

    try:
        records = anyio.run(_load)
    except Exception as exc:  # noqa: BLE001 - degrade to no-memory, never fail boot
        logger.warning(
            "memory load failed session=%s error_class=%s: %s (booting without memory)",
            config.session.session_id,
            type(exc).__name__,
            exc,
        )
        return store, None
    logger.info(
        "memory loaded session=%s records=%d", config.session.session_id, len(records)
    )
    return store, format_memory_preamble(records)


def _load_history(config: RunnerConfig) -> tuple[TranscriptStore, str | None]:
    """Resolve AGENTOS_HISTORY_REF and load this thread's transcript into a preamble.

    Mirrors ``_load_memory`` (ADR-0029): runs synchronously at boot so a bad ref
    fails the process visibly, but a transient load failure degrades to "no
    history" rather than blocking boot -- a thread must still run when its
    transcript store is briefly unavailable (the answer just lacks prior context).
    """

    store = resolve_history(config.history_ref, os.environ)

    async def _load() -> list[TurnRecord]:
        return await store.load()

    try:
        turns = anyio.run(_load)
    except Exception as exc:  # noqa: BLE001 - degrade to no-history, never fail boot
        logger.warning(
            "history load failed session=%s error_class=%s: %s (booting without history)",
            config.session.session_id,
            type(exc).__name__,
            exc,
        )
        return store, None
    logger.info(
        "history loaded session=%s turns=%d", config.session.session_id, len(turns)
    )
    return store, format_conversation_preamble(turns)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    fake_model = os.environ.get("AGENTOS_FAKE_MODEL", "").lower() in ("1", "true", "yes")
    logger.info("runner starting fake_model=%s", fake_model)
    # A real session authenticates from the SDK's own credential env; map the
    # forwarded ACI AGENTOS_CREDENTIALS reference onto it (a no-op for a fake
    # run, which needs no credential). Raises on an unsupported credential so the
    # process fails visibly before the port is up rather than after a real call.
    override = None
    if not fake_model:
        try:
            override = resolve_sdk_env(os.environ)
        except UnsupportedCredentialError as exc:
            logger.error("credential resolution failed: %s", exc)
            raise
    config = RunnerConfig.from_env(os.environ)
    logger.info(
        "runner configured session=%s model=%s port=%d",
        config.session.session_id,
        config.model,
        config.port,
    )
    memory_store, memory_preamble = _load_memory(config)
    history_store, conversation_preamble = _load_history(config)
    runner = build_runner(
        config,
        fake_model=fake_model,
        sdk_env=override,
        memory_store=memory_store,
        memory_preamble=memory_preamble,
        history_store=history_store,
        conversation_preamble=conversation_preamble,
    )
    app = create_app(runner, token=config.runner_token)

    async def _startup(_app: web.Application) -> None:
        try:
            await runner.start()
        except Exception as exc:
            logger.error("session start failed error_class=%s: %s", type(exc).__name__, exc)
            raise
        logger.info("session started session=%s", config.session.session_id)

    app.on_startup.append(_startup)
    web.run_app(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
