"""Runner entrypoint: build the session from the environment and serve the ACI.

Reads the ACI ``AGENTOS_*`` / ``OTEL_EXPORTER_OTLP_*`` env into a RunnerConfig,
wires the real claude-agent-sdk session (validated plugin bundle, budget, OTel),
and serves the HTTP channel. The session is started in ``on_startup`` so a plugin
or connect failure fails the process visibly rather than after the port is up.
"""

from __future__ import annotations

import os

from aiohttp import web

from .adapter import ClaudeAgentSession, ModelSession, build_options
from .config import RunnerConfig
from .fake import FakeModelSession
from .otel import RunTracer, build_tracer_provider
from .plugin import load_plugins
from .sdk_auth import resolve_model_credential
from .server import create_app
from .session import SessionRunner
from .side_effects import SideEffectClassifier


def build_runner(config: RunnerConfig, *, fake_model: bool = False) -> SessionRunner:
    """Wire a SessionRunner backed by a real claude-agent-sdk session.

    ``fake_model`` (env ``AGENTOS_FAKE_MODEL``) swaps in the scripted fake session
    so the image can round-trip a synthetic event with no model credential or
    network -- used for the container smoke and any offline exercise of the wiring
    (OTel export included). It never reaches the Anthropic API.
    """

    def factory() -> ModelSession:
        if fake_model:
            return FakeModelSession()
        plugins = load_plugins(config.session.plugin_dir)
        options = build_options(
            plugins=plugins,
            model=config.model,
            system_prompt=config.system_prompt,
            max_turns=config.max_turns,
            max_budget_usd=config.max_usd_per_day,
            resume=config.history_ref,
            task_budget_hint=config.session.budget.task_budget_hint,
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
        model=config.model,
    )


def main() -> None:
    fake_model = os.environ.get("AGENTOS_FAKE_MODEL", "").lower() in ("1", "true", "yes")
    # A real session authenticates from the SDK's own credential env; map the
    # forwarded ACI AGENTOS_CREDENTIALS reference onto it (a no-op for a fake
    # run, which needs no credential). Raises on an unsupported credential so the
    # process fails visibly before the port is up rather than after a real call.
    if not fake_model:
        resolve_model_credential(os.environ)
    config = RunnerConfig.from_env(os.environ)
    runner = build_runner(config, fake_model=fake_model)
    app = create_app(runner)

    async def _startup(_app: web.Application) -> None:
        await runner.start()

    app.on_startup.append(_startup)
    web.run_app(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
