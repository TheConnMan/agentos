"""Runner configuration: the typed ACI SessionConfig plus runner-local knobs.

``SessionConfig`` (frozen, from ``aci-protocol``) is the ACI session-setup
contract read from ``AGENTOS_*`` env. ``RunnerConfig`` wraps it with the handful
of runner-local settings that are not part of the frozen wire contract (model,
system prompt, turn cap, history ref, idempotent-tool override, listen port),
each read from its own env var so an operator can tune the harness without a
contract change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aci_protocol import SessionConfig

# Shared default turn cap read by both the runner env parse (RunnerConfig.from_env)
# and the OpenCode conformance harness, so both paths cap identically when
# AGENTOS_MAX_TURNS is unset.
DEFAULT_MAX_TURNS = 20


@dataclass(frozen=True)
class RunnerConfig:
    session: SessionConfig
    model: str | None
    system_prompt: str | None
    max_turns: int
    history_ref: str | None
    idempotent_tools: list[str] | None
    port: int
    runner_token: str | None

    @property
    def ceiling(self) -> int:
        """The per-run output-token ceiling from the ACI budget."""

        return self.session.budget.max_output_tokens_per_run

    @property
    def max_usd_per_day(self) -> float:
        return self.session.budget.max_usd_per_day

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RunnerConfig:
        """Parse a RunnerConfig from a process environment mapping.

        The ACI-frozen vars are parsed by ``SessionConfig.from_env``; a malformed
        or missing required var raises there. ``history_ref`` is read only from an
        explicit ``AGENTOS_HISTORY_REF``, which is an SDK ``resume`` session id
        (transcript- or session-store-backed). It is deliberately NOT derived from
        ``AGENTOS_MEMORY_REF``: the memory ref is an externalized-memory pointer
        (S3 path / API URL), a different concept the SDK cannot resume from. The
        worker/G1 is what turns externalized history into a resume id; the runner
        only accepts one (ADR-0003, stateless-first).
        """

        session = SessionConfig.from_env(env)
        idempotent_raw = env.get("AGENTOS_IDEMPOTENT_TOOLS")
        idempotent = (
            [t.strip() for t in idempotent_raw.split(",") if t.strip()]
            if idempotent_raw
            else None
        )
        return cls(
            session=session,
            model=env.get("AGENTOS_MODEL"),
            system_prompt=env.get("AGENTOS_SYSTEM_PROMPT"),
            max_turns=int(env.get("AGENTOS_MAX_TURNS", str(DEFAULT_MAX_TURNS))),
            history_ref=env.get("AGENTOS_HISTORY_REF"),
            idempotent_tools=idempotent,
            port=int(env.get("AGENTOS_RUNNER_PORT", "8080")),
            runner_token=env.get("AGENTOS_RUNNER_TOKEN") or None,
        )
