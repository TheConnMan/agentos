"""OpenCode runner entrypoint: build the session and serve the ACI.

Reads the ACI ``AGENTOS_*`` / ``OTEL_EXPORTER_OTLP_*`` env into a RunnerConfig,
wires the real OpenCode session (validated plugin bundle, budget, OTel), and
serves the HTTP channel. The session is started in ``on_startup`` so a plugin or
connect failure fails the process visibly rather than after the port is up.
"""

from __future__ import annotations

import logging
import os
import sys

from aiohttp import web

from ..config import RunnerConfig
from ..fake import FakeModelSession
from ..otel import RunTracer, build_tracer_provider
from ..sdk_auth import UnsupportedCredentialError
from ..server import create_app
from ..session import SessionRunner
from ..side_effects import SideEffectClassifier
from .auth import resolve_opencode_env
from .installer import OpenCodeBundleInstaller
from .session import OPENCODE_READONLY_TOOLS, OpenCodeModelSession

logger = logging.getLogger(__name__)


def build_runner(
    config: RunnerConfig,
    *,
    fake_model: bool = False,
    credential_env: dict[str, str] | None = None,
) -> SessionRunner:
    """Wire a SessionRunner backed by a real OpenCode session."""

    def factory() -> FakeModelSession | OpenCodeModelSession:
        if fake_model:
            return FakeModelSession()
        compiled = OpenCodeBundleInstaller().install(config.session.plugin_dir)
        cwd = compiled.workdir if compiled else None
        return OpenCodeModelSession(
            cwd=cwd,
            credential_env=credential_env,
            system_prompt=config.system_prompt,
            max_turns=config.max_turns,
            model=config.model,
        )

    provider = build_tracer_provider(
        config.session.otel,
        config.session.session_id,
        config.session.sandbox_id,
    )
    return SessionRunner(
        session_factory=factory,
        ceiling=config.ceiling,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(
            config.idempotent_tools
            if config.idempotent_tools is not None
            else OPENCODE_READONLY_TOOLS
        ),
        trace_name=f"agentos-run:{config.session.session_id}",
        session_id=config.session.session_id,
        model=config.model,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    fake_model = os.environ.get("AGENTOS_FAKE_MODEL", "").lower() in (
        "1",
        "true",
        "yes",
    )
    logger.info("OpenCode runner starting fake_model=%s", fake_model)
    config = RunnerConfig.from_env(os.environ)

    credential_env = None
    if not fake_model:
        if config.history_ref:
            raise RuntimeError(
                "OpenCode harness does not support AGENTOS_HISTORY_REF rehydration; "
                "refusing to silently cold-start"
            )
        try:
            credential_env = resolve_opencode_env(os.environ)
        except UnsupportedCredentialError as exc:
            logger.error("credential resolution failed: %s", exc)
            raise

    if config.max_usd_per_day > 0:
        logger.warning(
            "daily USD cap has no native OpenCode enforcement; "
            "the per-run output-token ceiling remains enforced"
        )

    logger.info(
        "OpenCode runner configured session=%s model=%s port=%d",
        config.session.session_id,
        config.model,
        config.port,
    )
    runner = build_runner(
        config,
        fake_model=fake_model,
        credential_env=credential_env,
    )
    app = create_app(runner, token=config.runner_token)

    async def _startup(_app: web.Application) -> None:
        try:
            await runner.start()
        except Exception as exc:
            logger.error(
                "session start failed error_class=%s: %s",
                type(exc).__name__,
                exc,
            )
            raise
        logger.info("session started session=%s", config.session.session_id)

    app.on_startup.append(_startup)
    web.run_app(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
