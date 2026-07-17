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


@dataclass(frozen=True)
class RunnerConfig:
    session: SessionConfig
    model: str | None
    system_prompt: str | None
    max_turns: int
    history_ref: str | None
    idempotent_tools: list[str] | None
    # Tool names whose calls require human approval (#245, ADR-0010). The
    # runner intercepts these proactively via the SDK can_use_tool callback
    # and ends the turn awaiting-approval instead of executing. Injected
    # per-agent by the worker binding as AGENTOS_APPROVAL_REQUIRED_TOOLS
    # (comma separated); None/empty means no permission gates and the
    # pre-gate bypass posture is preserved.
    approval_required_tools: list[str] | None
    # One-shot post-approval allowance (#430, ADR-0035): the single tool name a
    # resume-boot grant lets through exactly once on the boot turn. A runner-local
    # knob injected by the worker binding as AGENTOS_APPROVAL_GRANT_TOOL when it
    # boots the resume claim for a genuinely-approved permission-gate block;
    # None/empty means no grant and the ordinary deny-and-pause posture holds.
    approval_grant_tool: str | None
    # Turn-end reconciliation marker (#544, Decision A2), authority-free. The
    # worker injects AGENTOS_APPROVAL_RESUMED_KIND='policy' at resume boot to
    # record that the approval being resumed from was a POLICY gate. Unlike
    # AGENTOS_APPROVAL_GRANT_TOOL it confers NO authority -- it is a fact about
    # the past, used only to emit an observe-only warning when a resumed policy
    # turn takes no action. It must never influence can_use_tool.
    approval_resumed_kind: str | None
    # Opt-in false-completion check (#517), authority-free and observe-only. When
    # AGENTOS_FALSE_COMPLETION_CHECK is truthy, a turn that ends DONE with a
    # substantive answer but ZERO tool calls emits a non-terminal warning frame.
    # Default off, exactly like approval_resumed_kind's observe-only pattern; it
    # never influences can_use_tool or the final's status.
    false_completion_check: bool
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
        explicit ``AGENTOS_HISTORY_REF``, the URL of this thread's transcript
        namespace on the state API (ADR-0029, resolved by ``history.py`` into a
        ``TranscriptStore`` and delivered as a boot preamble). It is deliberately
        NOT derived from ``AGENTOS_MEMORY_REF``: memory is per-agent durable
        lessons, history is this thread's conversation (ADR-0025 keeps them
        distinct). Both live outside the sandbox and are rehydrated at boot
        (ADR-0003, stateless-first).
        """

        session = SessionConfig.from_env(env)
        idempotent_raw = env.get("AGENTOS_IDEMPOTENT_TOOLS")
        idempotent = (
            [t.strip() for t in idempotent_raw.split(",") if t.strip()]
            if idempotent_raw
            else None
        )
        approval_raw = env.get("AGENTOS_APPROVAL_REQUIRED_TOOLS")
        approval_required = (
            [t.strip() for t in approval_raw.split(",") if t.strip()]
            if approval_raw
            else None
        )
        grant_raw = env.get("AGENTOS_APPROVAL_GRANT_TOOL")
        approval_grant_tool = grant_raw.strip() if grant_raw and grant_raw.strip() else None
        resumed_raw = env.get("AGENTOS_APPROVAL_RESUMED_KIND")
        approval_resumed_kind = (
            resumed_raw.strip() if resumed_raw and resumed_raw.strip() else None
        )
        false_completion_raw = env.get("AGENTOS_FALSE_COMPLETION_CHECK", "")
        false_completion_check = false_completion_raw.strip().lower() in ("1", "true", "yes")
        return cls(
            session=session,
            model=env.get("AGENTOS_MODEL"),
            system_prompt=env.get("AGENTOS_SYSTEM_PROMPT"),
            max_turns=int(env.get("AGENTOS_MAX_TURNS", "20")),
            history_ref=env.get("AGENTOS_HISTORY_REF"),
            idempotent_tools=idempotent,
            approval_required_tools=approval_required,
            approval_grant_tool=approval_grant_tool,
            approval_resumed_kind=approval_resumed_kind,
            false_completion_check=false_completion_check,
            port=int(env.get("AGENTOS_RUNNER_PORT", "8080")),
            runner_token=env.get("AGENTOS_RUNNER_TOKEN") or None,
        )
