"""Bundle PreToolUse hook consumption (#272): manifest -> SDK HookMatcher."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from agentos_runner import load_bundle_hooks


def _bundle(tmp_path: Path, hooks: object) -> str:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo", "hooks": hooks}), encoding="utf-8"
    )
    return str(tmp_path)


def test_no_bundle_or_no_hooks_is_none(tmp_path: Path) -> None:
    assert load_bundle_hooks(None) is None
    assert load_bundle_hooks("") is None
    # A manifest without hooks.
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "demo"}', encoding="utf-8"
    )
    assert load_bundle_hooks(str(tmp_path)) is None


def test_pretooluse_command_hook_becomes_matcher(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]},
    )
    hooks = load_bundle_hooks(plugin_dir)
    assert hooks is not None
    matchers = hooks["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0].matcher == "Bash"
    assert len(matchers[0].hooks) == 1  # one callback wrapping the command(s)


def test_non_pretooluse_events_are_ignored(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {"PostToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]},
    )
    assert load_bundle_hooks(plugin_dir) is None


def test_non_command_hook_actions_are_skipped(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "someFutureType"}]}]},
    )
    assert load_bundle_hooks(plugin_dir) is None


def test_hook_callback_denies_on_exit_2(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "echo blocked 1>&2; exit 2"}]}
            ]
        },
    )
    hooks = load_bundle_hooks(plugin_dir)
    assert hooks is not None
    callback = hooks["PreToolUse"][0].hooks[0]

    async def go() -> dict:
        return await callback({"tool_name": "Bash", "tool_input": {}}, "tuid", {})

    out = anyio.run(go)
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "blocked" in decision["permissionDecisionReason"]


def test_hook_callback_allows_on_exit_0(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "exit 0"}]}]},
    )
    hooks = load_bundle_hooks(plugin_dir)
    assert hooks is not None
    callback = hooks["PreToolUse"][0].hooks[0]

    async def go() -> dict:
        return await callback({"tool_name": "Bash", "tool_input": {}}, "tuid", {})

    assert anyio.run(go) == {}


def test_first_denying_command_short_circuits(tmp_path: Path) -> None:
    plugin_dir = _bundle(
        tmp_path,
        {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "exit 0"},
                        {"type": "command", "command": "echo no 1>&2; exit 2"},
                    ]
                }
            ]
        },
    )
    hooks = load_bundle_hooks(plugin_dir)
    assert hooks is not None
    callback = hooks["PreToolUse"][0].hooks[0]

    async def go() -> dict:
        return await callback({"tool_name": "Bash", "tool_input": {}}, "tuid", {})

    out = anyio.run(go)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
