"""Offline unit tests for the OpenCode runner server entrypoint (issue #312).

Pins the wiring contract of ``agentos_runner.opencode.__main__`` -- the mirror of
``agentos_runner.__main__`` that composes the OpenCode harness instead of the
claude-agent-sdk one. Everything here is offline: the CI anchor (the fake
round-trip) must pass on a machine with no ``opencode`` binary, proven by
monkeypatching ``_find_opencode`` to raise and showing it is never consulted.

House rules honored: the only mocks are monkeypatches of env / module-namespace
collaborators (``_find_opencode``, ``OpenCodeModelSession``, ``SideEffectClassifier``,
``create_app``, ``web.run_app``); credential-shaped literals are hoisted to named
constants built by concatenation so the secrets hook never trips on a quoted
``sk-`` string.
"""

from __future__ import annotations

import logging
from pathlib import Path

import agentos_runner.opencode.__main__ as oc_main
import agentos_runner.opencode.session as oc_session
import anyio
import pytest
from aci_protocol import Event, SessionStatus, parse_ndjson
from agentos_runner import (
    BUDGET_CLASSIFICATION,
    PluginBundleError,
    SideEffectClassifier,
)
from agentos_runner.config import RunnerConfig
from agentos_runner.fake import FakeModelSession
from agentos_runner.opencode import OPENCODE_READONLY_TOOLS
from agentos_runner.opencode.__main__ import build_runner, main
from agentos_runner.sdk_auth import UnsupportedCredentialError

_HERE = Path(__file__).parent
_LIVE_BUNDLE = _HERE / "fixtures" / "opencode_live_bundle"

# The frozen ACI env shape, mirroring runner/tests/test_config.py's _BASE.
_BASE = {
    "AGENTOS_PLUGIN_DIR": "/bundle",
    "AGENTOS_SESSION_ID": "sess-1",
    "AGENTOS_SANDBOX_ID": "sbx-1",
    "AGENTOS_BUDGET": '{"max_output_tokens_per_run": 1000, "max_usd_per_day": 5.0}',
}

# Runner-local vars set per-test on top of _BASE; delenv'd in _set_env so a stray
# ambient value never contaminates a main() test.
_OPTIONAL_VARS = (
    "AGENTOS_FAKE_MODEL",
    "AGENTOS_HISTORY_REF",
    "AGENTOS_CREDENTIALS",
    "AGENTOS_IDEMPOTENT_TOOLS",
    "AGENTOS_RUNNER_TOKEN",
    "AGENTOS_MODEL",
    "AGENTOS_SYSTEM_PROMPT",
    "AGENTOS_MAX_TURNS",
)

# A per-sandbox bearer token (not a model credential): a plain identifier.
_RUNNER_TOKEN = "test-token-" + "xyz"

# Credential-shaped literals, concatenated so the secrets hook sees identifiers,
# not quoted ``sk-`` strings. The unsupported one classifies as an Anthropic API
# key, which OpenCode's binder rejects; the OpenRouter dict is passed straight
# through as ``credential_env`` (never re-classified by build_runner).
_UNSUPPORTED_CRED = "sk-" + "ant-" + "a" * 24
_OPENROUTER_ENV = {"OPENROUTER_API_KEY": "sk-" + "or-" + "v1-" + "z" * 24}


def _config(**overrides: str) -> RunnerConfig:
    return RunnerConfig.from_env(dict(_BASE, **overrides))


def _event() -> Event:
    return Event(type="message", text="hi", user="U1", ts="1.0")


def _drive_turn(runner: object) -> list:
    """Start a runner, drive one turn, and return the parsed ACI events."""

    lines: list[str] = []

    async def go() -> None:
        await runner.start()  # type: ignore[attr-defined]
        async for line in runner.run_turn(_event()):  # type: ignore[attr-defined]
            lines.append(line)

    anyio.run(go)
    return parse_ndjson("".join(lines))


def _raise_find_opencode(*_args: object, **_kwargs: object) -> str:
    raise AssertionError("opencode binary must not be looked up on this path")


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Install a clean ACI env into os.environ for a main() test."""

    for key, value in _BASE.items():
        monkeypatch.setenv(key, value)
    for key in _OPTIONAL_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


# --------------------------------------------------------------------------- #
# 1. Fake round-trip -- the CI anchor: passes with no opencode binary present.
# --------------------------------------------------------------------------- #


def test_fake_round_trip_needs_no_opencode_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fake factory must never touch _find_opencode; raise from it to prove the
    # offline path is a true no-op that a binary-less CI machine can run.
    monkeypatch.setattr(oc_session, "_find_opencode", _raise_find_opencode)
    runner = build_runner(_config(), fake_model=True)
    events = _drive_turn(runner)
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE


# --------------------------------------------------------------------------- #
# 2. Classifier declaration -- OpenCode read-only set, is-not-None override edge.
# --------------------------------------------------------------------------- #


def _capture_classifier(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict[str, object] = {}
    real = oc_main.SideEffectClassifier

    def _cap(tools: object) -> SideEffectClassifier:
        captured["tools"] = tools
        instance = real(tools)
        captured["instance"] = instance
        return instance

    monkeypatch.setattr(oc_main, "SideEffectClassifier", _cap)
    return captured


def test_default_classifier_declares_opencode_readonly_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_classifier(monkeypatch)
    build_runner(_config(), fake_model=True)
    # The default allowlist must be OpenCode's lowercase set, not the Claude one.
    assert captured["tools"] == OPENCODE_READONLY_TOOLS
    classifier = captured["instance"]
    assert not classifier.is_side_effecting("read")  # type: ignore[attr-defined]
    assert classifier.is_side_effecting("write")  # type: ignore[attr-defined]


def test_empty_idempotent_override_denies_every_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AGENTOS_IDEMPOTENT_TOOLS="," parses to [] (an empty allowlist). The
    # entrypoint must guard with ``is not None`` (not ``or``) so [] reaches the
    # classifier and denies everything, rather than falling back to the OpenCode
    # declaration.
    captured = _capture_classifier(monkeypatch)
    build_runner(_config(AGENTOS_IDEMPOTENT_TOOLS=","), fake_model=True)
    assert captured["tools"] == []
    assert captured["instance"].is_side_effecting("read")  # type: ignore[attr-defined]


def test_explicit_idempotent_override_replaces_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_classifier(monkeypatch)
    build_runner(_config(AGENTOS_IDEMPOTENT_TOOLS="Read, Custom"), fake_model=True)
    assert captured["tools"] == ["Read", "Custom"]


# --------------------------------------------------------------------------- #
# 3. Session wiring -- compiled cwd, prompt/turns/model from config, credentials.
# --------------------------------------------------------------------------- #


def _capture_session(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    class _CapturingSession(FakeModelSession):
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            super().__init__()

    monkeypatch.setattr(oc_main, "OpenCodeModelSession", _CapturingSession)
    return calls


def _start_and_close(runner: object) -> None:
    async def go() -> None:
        await runner.start()  # type: ignore[attr-defined]
        await runner.close()  # type: ignore[attr-defined]

    anyio.run(go)


def test_session_wiring_binds_compiled_cwd_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_session(monkeypatch)
    config = _config(
        AGENTOS_PLUGIN_DIR=str(_LIVE_BUNDLE),
        AGENTOS_SYSTEM_PROMPT="be terse",
        AGENTOS_MAX_TURNS="7",
        AGENTOS_MODEL="z-ai/glm-4.6",
    )
    runner = build_runner(config, fake_model=False, credential_env=dict(_OPENROUTER_ENV))
    _start_and_close(runner)

    assert len(calls) == 1
    kwargs = calls[0]
    # The compiled bundle workdir is bound as the session cwd -- an existing dir
    # holding opencode.json (where OpenCode discovers the compiled config).
    cwd = kwargs["cwd"]
    assert cwd is not None
    assert (Path(cwd) / "opencode.json").is_file()
    assert kwargs["system_prompt"] == "be terse"
    assert kwargs["max_turns"] == 7
    assert kwargs["model"] == "z-ai/glm-4.6"
    assert kwargs["credential_env"] == _OPENROUTER_ENV


def test_session_wiring_bundleless_passes_cwd_none_and_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_session(monkeypatch)
    # An empty plugin dir compiles to no bundle -> cwd None (the session mkdtemps
    # its own workdir); an unset AGENTOS_MODEL passes model=None (the session's
    # own default applies).
    config = _config(AGENTOS_PLUGIN_DIR="")
    runner = build_runner(config, fake_model=False, credential_env={})
    _start_and_close(runner)

    assert len(calls) == 1
    assert calls[0]["cwd"] is None
    assert calls[0]["model"] is None


# --------------------------------------------------------------------------- #
# 4. Fail-loud in main() -- before the port is up.
# --------------------------------------------------------------------------- #


def _stub_serve(monkeypatch: pytest.MonkeyPatch) -> list:
    served: list = []
    monkeypatch.setattr(oc_main.web, "run_app", lambda app, **_k: served.append(app))
    return served


def test_main_history_ref_refuses_to_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenCode has no resume; a set AGENTOS_HISTORY_REF must fail loud at startup
    # rather than silently cold-starting a session the worker asked to rehydrate.
    _set_env(monkeypatch, AGENTOS_HISTORY_REF="s3://hist")
    served = _stub_serve(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        main()
    # Not the UnsupportedCredentialError subclass -- the plain history refusal.
    assert excinfo.type is RuntimeError
    assert "history" in str(excinfo.value).lower()
    assert not served


def test_main_unsupported_credential_raises_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, AGENTOS_CREDENTIALS=_UNSUPPORTED_CRED)
    served = _stub_serve(monkeypatch)
    with pytest.raises(UnsupportedCredentialError):
        main()
    assert not served


def test_main_fake_mode_ignores_history_ref_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fake path resolves no credential and rehydrates nothing, so neither an
    # unsupported credential nor a history ref stops an offline fake boot.
    _set_env(
        monkeypatch,
        AGENTOS_FAKE_MODEL="1",
        AGENTOS_HISTORY_REF="s3://hist",
        AGENTOS_CREDENTIALS=_UNSUPPORTED_CRED,
    )
    monkeypatch.setattr(oc_session, "_find_opencode", _raise_find_opencode)
    served = _stub_serve(monkeypatch)
    main()
    assert len(served) == 1


# --------------------------------------------------------------------------- #
# 4b. The runner token is passed through to the ACI app (regression: #63).
# --------------------------------------------------------------------------- #


def test_main_passes_runner_token_to_create_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #63: the ACI HTTP channel must never ship unauthenticated. The entrypoint
    # reuses create_app, so it must forward config.runner_token; dropping it
    # silently reopens the unauthenticated hole.
    _set_env(monkeypatch, AGENTOS_FAKE_MODEL="1", AGENTOS_RUNNER_TOKEN=_RUNNER_TOKEN)
    captured: dict[str, object] = {}
    real_create_app = oc_main.create_app

    def _cap(runner: object, token: str | None = None) -> object:
        captured["token"] = token
        return real_create_app(runner, token=token)

    monkeypatch.setattr(oc_main, "create_app", _cap)
    served = _stub_serve(monkeypatch)
    main()
    assert captured["token"] == _RUNNER_TOKEN
    assert len(served) == 1


# --------------------------------------------------------------------------- #
# 5. Budget ceiling flows from AGENTOS_BUDGET.max_output_tokens_per_run.
# --------------------------------------------------------------------------- #


def test_budget_ceiling_flows_from_env_and_halts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oc_session, "_find_opencode", _raise_find_opencode)
    # A ceiling of 1 is exceeded by the fake turn's 8 output tokens, so the run
    # halts with a classified-failure final carrying the budget classification.
    config = _config(
        AGENTOS_BUDGET='{"max_output_tokens_per_run": 1, "max_usd_per_day": 5.0}'
    )
    runner = build_runner(config, fake_model=True)
    events = _drive_turn(runner)
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    assert any(
        e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events
    )


def test_budget_headroom_completes_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc_session, "_find_opencode", _raise_find_opencode)
    config = _config(
        AGENTOS_BUDGET='{"max_output_tokens_per_run": 1000, "max_usd_per_day": 5.0}'
    )
    runner = build_runner(config, fake_model=True)
    events = _drive_turn(runner)
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE


# --------------------------------------------------------------------------- #
# 6. A daily-USD cap warns (OpenCode has no native enforcement of it).
# --------------------------------------------------------------------------- #


def test_main_warns_on_usd_cap_without_native_enforcement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # _BASE carries max_usd_per_day=5.0. The Claude path hands the daily USD cap
    # to the SDK; OpenCode cannot enforce it, so the entrypoint must warn rather
    # than silently pretend the cap is honored (only the per-run token ceiling is).
    _set_env(monkeypatch)
    served = _stub_serve(monkeypatch)
    with caplog.at_level(logging.WARNING):
        main()
    assert any("usd" in record.getMessage().lower() for record in caplog.records)
    assert len(served) == 1


# --------------------------------------------------------------------------- #
# Invalid bundle fails closed at start -- before any capability-less boot.
# --------------------------------------------------------------------------- #


def test_invalid_bundle_raises_plugin_bundle_error_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ensure_valid_bundle rejects a manifest-less dir; that PluginBundleError must
    # propagate out of session start (never a capability-less boot), and it fires
    # before the binary is ever needed.
    monkeypatch.setattr(oc_session, "_find_opencode", _raise_find_opencode)
    bad_bundle = tmp_path / "bundle"
    bad_bundle.mkdir()
    (bad_bundle / "random.txt").write_text("not a bundle", encoding="utf-8")
    runner = build_runner(_config(AGENTOS_PLUGIN_DIR=str(bad_bundle)), fake_model=False)

    async def go() -> None:
        await runner.start()

    with pytest.raises(PluginBundleError):
        anyio.run(go)
