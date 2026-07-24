"""RunnerConfig consumes the declared BootEnv, with today's exact semantics.

#488 makes ``BootEnv.from_env`` the runner's single parse of the boot env and
deletes ``RunnerConfig.from_env``'s bare ``CURIE_*`` literals. The config it
produces from a given env must not change, so this module pins the parse
per-knob against TODAY's behavior.

The parse tolerance is deliberately NON-uniform and each knob keeps what it has:
``CURIE_MAX_TURNS`` and ``CURIE_RUNNER_PORT`` RAISE on garbage (a bare
``int()`` today), while the history-window knobs DEGRADE to their default on
garbage and on a nonpositive value. Unifying them would be a behavior change
wearing a consistency costume, so the asymmetry is pinned on both sides.

``test_config.py`` covers the pre-#488 surface; this module covers what the
conversion must preserve and what it must remove.
"""

from __future__ import annotations

import dataclasses

import pytest
from curie_runner import RunnerConfig
from pydantic import ValidationError

_BASE = {
    "CURIE_PLUGIN_DIR": "/bundle",
    "CURIE_SESSION_ID": "sess-1",
    "CURIE_SANDBOX_ID": "sbx-1",
    "CURIE_BUDGET": '{"max_output_tokens_per_run": 1000, "max_usd_per_day": 5.0}',
}


def _field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(RunnerConfig)}


def test_full_boot_env_parses_to_the_same_config() -> None:
    """Every knob a real bound claim sets, read back off one config."""

    env = dict(
        _BASE,
        CURIE_MEMORY_REF="http://api/agents/a/state/memory",
        CURIE_CREDENTIALS="cred-1",
        CURIE_HISTORY_REF="http://api/agents/a/state/transcript/t",
        CURIE_RUNNER_TOKEN="tok-1",
        CURIE_MODEL="agent-pinned",
        CURIE_APPROVAL_REQUIRED_TOOLS="Bash, Write ,",
        CURIE_APPROVAL_GRANT_TOOL="  Bash  ",
        CURIE_APPROVAL_RESUMED_KIND="  policy  ",
        CURIE_MAX_TURNS="7",
        CURIE_RUNNER_PORT="9090",
    )

    config = RunnerConfig.from_env(env)

    assert config.session.plugin_dir == "/bundle"
    assert config.session.session_id == "sess-1"
    assert config.session.sandbox_id == "sbx-1"
    assert config.session.memory_ref == "http://api/agents/a/state/memory"
    assert config.session.credentials_ref == "cred-1"
    assert config.ceiling == 1000
    assert config.max_usd_per_day == 5.0
    assert config.history_ref == "http://api/agents/a/state/transcript/t"
    assert config.runner_token == "tok-1"
    assert config.model == "agent-pinned"
    # Comma-joined names: stripped, blanks dropped.
    assert config.approval_required_tools == ["Bash", "Write"]
    # The approval markers DO strip today; the token/ref knobs do not.
    assert config.approval_grant_tool == "Bash"
    assert config.approval_resumed_kind == "policy"
    assert config.max_turns == 7
    assert config.port == 9090


def test_defaults_when_the_operator_knobs_are_absent() -> None:
    config = RunnerConfig.from_env(dict(_BASE))

    assert config.max_turns == 20
    assert config.port == 8080
    assert config.history_ref is None
    assert config.runner_token is None
    assert config.approval_required_tools is None
    assert config.approval_grant_tool is None
    assert config.approval_resumed_kind is None


def test_approval_markers_blank_is_unset() -> None:
    env = dict(_BASE, CURIE_APPROVAL_GRANT_TOOL="   ", CURIE_APPROVAL_RESUMED_KIND="")
    config = RunnerConfig.from_env(env)

    # A blank grant must never read as a tool named "" that the gate then lets
    # through; a blank resumed-kind must not claim a policy resume happened.
    assert config.approval_grant_tool is None
    assert config.approval_resumed_kind is None


def test_approval_required_tools_all_blank_is_no_gates() -> None:
    config = RunnerConfig.from_env(dict(_BASE, CURIE_APPROVAL_REQUIRED_TOOLS=" , , "))

    assert not config.approval_required_tools


def test_runner_token_empty_string_is_unset_not_empty() -> None:
    # The token is enforced only when configured (#63), so an empty value must
    # read as unset rather than turning on enforcement with an unusable token.
    assert RunnerConfig.from_env(dict(_BASE, CURIE_RUNNER_TOKEN="")).runner_token is None


def test_max_turns_raises_on_garbage() -> None:
    # Today's bare int(): an operator typo fails the boot loudly rather than
    # silently running with a different turn cap.
    with pytest.raises(ValueError):
        RunnerConfig.from_env(dict(_BASE, CURIE_MAX_TURNS="lots"))


def test_runner_port_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        RunnerConfig.from_env(dict(_BASE, CURIE_RUNNER_PORT="eighty"))


def test_history_window_comes_through_the_declared_surface() -> None:
    """The window is an operator knob on the boot env, not a bare os.environ read.

    Reading it off the process env at the call site meant the value could not be
    seen, tested, or overridden through the config the rest of the boot uses.
    """

    config = RunnerConfig.from_env(
        dict(_BASE, CURIE_HISTORY_MAX_TURNS="4", CURIE_HISTORY_MAX_BYTES="2048")
    )

    assert config.history_max_turns == 4
    assert config.history_max_bytes == 2048


@pytest.mark.parametrize("raw", ["", "   ", "twelve", "0", "-3", "1.5"])
def test_history_window_degrades_rather_than_raising(raw: str) -> None:
    """A typo in an operator's extraEnv must not become a boot crash.

    A nonpositive window is rejected the same way: max_turns=0 slices every turn
    and a nonpositive byte budget can never be met, so both are meaningless.
    None hands the consumer its own default.
    """

    config = RunnerConfig.from_env(
        dict(_BASE, CURIE_HISTORY_MAX_TURNS=raw, CURIE_HISTORY_MAX_BYTES=raw)
    )

    assert config.history_max_turns is None
    assert config.history_max_bytes is None


def test_malformed_budget_still_raises() -> None:
    with pytest.raises(ValidationError):
        RunnerConfig.from_env(dict(_BASE, CURIE_BUDGET="{not json"))


def test_system_prompt_is_no_longer_a_config_surface() -> None:
    """The bundle is the declared system-prompt surface and always wins (AC5).

    CURIE_SYSTEM_PROMPT let an operator env silently replace the prompt
    versioned with the agent, which is unauditable: the bundle says one thing and
    the sandbox runs another.
    """

    assert "system_prompt" not in _field_names()

    config = RunnerConfig.from_env(dict(_BASE, CURIE_SYSTEM_PROMPT="you-are-overridden"))

    # The env value must reach no field at all: if it lands anywhere on the
    # config, some boot path can still prefer it over the bundle's prompt.
    assert "you-are-overridden" not in repr(dataclasses.astuple(config))


def test_idempotent_tools_is_no_longer_a_config_surface() -> None:
    """The env override was never wired to a consumer (AC5).

    An unwired widening knob on a deny-by-default classifier is worse than none:
    it reads as an escape hatch that silently does nothing.
    """

    assert "idempotent_tools" not in _field_names()

    config = RunnerConfig.from_env(dict(_BASE, CURIE_IDEMPOTENT_TOOLS="Read,Bash"))

    assert "Read" not in repr(dataclasses.astuple(config))
