"""Pure builders for the OpenCode subprocess env + inline config (issue #311).

Two module-level helpers the plan extracts from ``OpenCodeModelSession`` so they
are testable without spawning ``opencode serve`` (a live server is driver-only --
sub-agent sandboxes kill it):

- ``build_subprocess_env(base_env, credential_env, config_content)`` sanitizes
  inherited credential vars, injects exactly the bound credential, augments PATH,
  and sets ``OPENCODE_CONFIG_CONTENT`` only when there is content.
- ``build_config_content(system_prompt, max_turns)`` serializes the
  ``agent.build`` prompt/steps carrier, or ``None`` when nothing is set.

AC3 lives here too: ambient ``OPENROUTER_*`` must never reach the subprocess
without an explicit binding (the deleted spike fallback).
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest
from agentos_runner.opencode.session import (
    build_config_content,
    build_prompt_body,
    build_subprocess_env,
)

FAKE_OPENROUTER_KEY = "sk-or-" + "v1-FAKE-key"

# Every credential var the builder must strip from an inherited environment.
CREDENTIAL_VARS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AGENTOS_CREDENTIALS",
    "OPENCODE_CONFIG_CONTENT",
]


def _polluted_base() -> dict[str, str]:
    """A base env carrying ambient values for every credential var, plus HOME/PATH."""

    base = {"HOME": "/home/runner", "PATH": "/usr/bin"}
    for var in CREDENTIAL_VARS:
        base[var] = "AMBIENT-" + var
    return base


# --- subprocess env: sanitize + inject ------------------------------------------


def test_env_builder_strips_every_ambient_credential_var() -> None:
    out = build_subprocess_env(_polluted_base(), None, None)
    for var in CREDENTIAL_VARS:
        assert var not in out, var


def test_env_builder_injects_only_the_bound_credential() -> None:
    out = build_subprocess_env(
        _polluted_base(), {"OPENROUTER_API_KEY": FAKE_OPENROUTER_KEY}, None
    )
    assert out["OPENROUTER_API_KEY"] == FAKE_OPENROUTER_KEY
    # No other credential var leaks from the ambient base.
    for var in CREDENTIAL_VARS:
        if var == "OPENROUTER_API_KEY":
            continue
        assert var not in out, var


def test_env_builder_passes_through_unrelated_vars() -> None:
    out = build_subprocess_env({"HOME": "/home/runner", "PATH": "/usr/bin"}, None, None)
    assert out["HOME"] == "/home/runner"


def test_env_builder_prefixes_opencode_and_bun_bin_onto_path() -> None:
    out = build_subprocess_env({"PATH": "/usr/bin"}, None, None)
    assert ".opencode/bin" in out["PATH"]
    assert ".bun/bin" in out["PATH"]
    # The opencode runtime dir must be a PATH *prefix*, ahead of the inherited entries.
    assert out["PATH"].index(".opencode/bin") < out["PATH"].index("/usr/bin")


def test_ac3_ambient_openrouter_never_reaches_subprocess_without_binding() -> None:
    # The removed spike fallback used to forward host OPENROUTER_API_KEY /
    # OPENROUTER_TOKEN. With no explicit binding the subprocess must get no
    # OpenRouter credential at all.
    base = {
        "PATH": "/usr/bin",
        "OPENROUTER_API_KEY": "AMBIENT-key",
        "OPENROUTER_TOKEN": "AMBIENT-token",
    }
    out = build_subprocess_env(base, None, None)
    assert "OPENROUTER_API_KEY" not in out
    assert "OPENROUTER_TOKEN" not in out


def test_env_builder_sets_config_content_when_present() -> None:
    content = '{"agent": {"build": {"prompt": "p"}}}'
    out = build_subprocess_env({"PATH": "/usr/bin"}, None, content)
    assert out["OPENCODE_CONFIG_CONTENT"] == content


def test_env_builder_omits_config_content_when_absent() -> None:
    out = build_subprocess_env({"PATH": "/usr/bin"}, None, None)
    # Absent, not empty-string.
    assert "OPENCODE_CONFIG_CONTENT" not in out


def test_source_no_longer_reads_ambient_openrouter_token() -> None:
    # AC3 done-when: `grep OPENROUTER_TOKEN runner/src/.../session.py` is empty.
    source = pathlib.Path(
        "runner/src/agentos_runner/opencode/session.py"
    ).read_text(encoding="utf-8")
    assert "OPENROUTER_TOKEN" not in source


# --- config content composition -------------------------------------------------


def test_config_content_prompt_and_steps() -> None:
    content = build_config_content("You are terse.", 3)
    assert content is not None
    assert json.loads(content) == {
        "agent": {"build": {"prompt": "You are terse.", "steps": 3}}
    }


def test_config_content_prompt_only_omits_steps() -> None:
    content = build_config_content("only prompt", None)
    assert content is not None
    assert json.loads(content) == {"agent": {"build": {"prompt": "only prompt"}}}


def test_config_content_steps_only_omits_prompt() -> None:
    content = build_config_content(None, 5)
    assert content is not None
    assert json.loads(content) == {"agent": {"build": {"steps": 5}}}


def test_config_content_empty_prompt_treated_as_unset() -> None:
    content = build_config_content("", 2)
    assert content is not None
    assert json.loads(content) == {"agent": {"build": {"steps": 2}}}


def test_config_content_none_when_nothing_to_set() -> None:
    assert build_config_content(None, None) is None
    assert build_config_content("", None) is None


def test_config_content_nonpositive_steps_omitted_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # steps has schema exclusiveMinimum 0; a <=0 value would fail serve config
    # validation, so it is dropped (with a warning) rather than emitted.
    with caplog.at_level(logging.WARNING):
        content = build_config_content("p", 0)
    assert content is not None
    parsed = json.loads(content)
    assert parsed == {"agent": {"build": {"prompt": "p"}}}
    assert "steps" not in parsed["agent"]["build"]
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_config_content_nonpositive_steps_alone_is_none() -> None:
    assert build_config_content(None, 0) is None


def test_config_content_roundtrips_json_metacharacters() -> None:
    prompt = 'Include "quotes", \n newlines, and {braces} verbatim.'
    content = build_config_content(prompt, 1)
    assert content is not None
    parsed = json.loads(content)
    assert parsed["agent"]["build"]["prompt"] == prompt
    assert parsed["agent"]["build"]["steps"] == 1


# --- prompt body composition ----------------------------------------------------


def test_prompt_body_targets_build_agent_and_carries_model_and_text() -> None:
    # The body MUST name the build agent, or the OPENCODE_CONFIG_CONTENT carrier
    # (which nests prompt/steps under agent.build) never applies to the turn.
    body = build_prompt_body("openrouter", "z-ai/glm-4.6", "hello")
    assert body == {
        "agent": "build",
        "model": {"providerID": "openrouter", "modelID": "z-ai/glm-4.6"},
        "parts": [{"type": "text", "text": "hello"}],
    }
