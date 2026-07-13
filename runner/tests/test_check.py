"""Tests for the offline MCP load check (`agentos_runner.check`, issue #337).

Test-first: these pin the frozen runner<->CLI JSON seam (plan Section 3) and the
verdict rules. Until ``check.py`` exists the module import fails collection --
that is the intended red for the Stage-2 test-writer pass.

Mocking discipline (plan Section 5): nothing here is mocked. Tests 1-3 are pure
functions over real fixture dirs and literal ``McpServerStatus``-shaped dicts;
test 4 runs the module as a subprocess; test 5 drives the *real* Claude Code
loader (gated on the ``claude`` CLI being present).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from agentos_runner.check import evaluate, extract_declared, run_check

_HERE = Path(__file__).resolve().parent
_FIXTURES = _HERE / "fixtures"
_MCP_GREEN = _FIXTURES / "mcp_green"
_MCP_RED_POINTER = _FIXTURES / "mcp_red_pointer"
_MCP_RED_BROKEN = _FIXTURES / "mcp_red_broken"
_PLUGIN_FORMAT_FIXTURES = _HERE.parents[1] / "packages/plugin-format/tests/fixtures"

_CLAUDE_ON_PATH = shutil.which("claude") is not None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _declared(name: str, source: str = "plugin.json", form: str = "inline") -> dict:
    return {"name": name, "source": source, "form": form}


def _tool(name: str) -> dict:
    return {"name": name}


def _status(
    name: str,
    *,
    status: str = "connected",
    scope: str | None = "dynamic",
    tools: list[dict] | None = None,
    error: str | None = None,
) -> dict:
    entry: dict = {"name": name, "status": status, "tools": list(tools or [])}
    if scope is not None:
        entry["scope"] = scope
    if error is not None:
        entry["error"] = error
    return entry


def _write_bundle(
    root: Path, manifest: dict, *, mcp_files: dict[str, dict] | None = None
) -> Path:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    for rel, payload in (mcp_files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Test 1 -- declared extraction over real bundle dirs (no mocks)
# --------------------------------------------------------------------------- #
def test_extract_inline_object_form() -> None:
    # Canonical declared shape: exactly {name, source, form}, nothing else.
    assert extract_declared(str(_MCP_GREEN)) == [
        {"name": "green-probe", "source": "plugin.json", "form": "inline"}
    ]


def test_extract_string_pointer_form() -> None:
    declared = extract_declared(str(_MCP_RED_POINTER))
    by_name = {d["name"]: d for d in declared}
    assert "pointer-probe" in by_name
    assert by_name["pointer-probe"]["form"] == "string_pointer"
    assert by_name["pointer-probe"]["source"] == "plugin.json"


def test_extract_string_pointer_missing_file_is_red_path(tmp_path: Path) -> None:
    # Intent still surfaces (a string-pointer declaration is visible) even though
    # the pointed file does not exist; and the missing target drives a red verdict.
    bundle = _write_bundle(
        tmp_path / "missing_ptr", {"name": "missing-ptr", "mcpServers": "config/nope.json"}
    )
    declared = extract_declared(str(bundle))
    assert declared, "a string-pointer declaration must still surface as a declared entry"
    assert all(d["form"] == "string_pointer" for d in declared)
    result = evaluate(declared, [])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_extract_bare_mcp_json_form(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path / "bare",
        {"name": "bare-bundle"},
        mcp_files={".mcp.json": {"mcpServers": {"bare-probe": {"command": "python3"}}}},
    )
    declared = extract_declared(str(bundle))
    by_name = {d["name"]: d for d in declared}
    assert "bare-probe" in by_name
    assert by_name["bare-probe"]["form"] == "bare_file"
    assert by_name["bare-probe"]["source"] == ".mcp.json"


def test_extract_no_mcp_anywhere_is_empty(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "nomcp", {"name": "no-mcp-bundle"})
    assert extract_declared(str(bundle)) == []


# --------------------------------------------------------------------------- #
# Test 2 -- verdict pure function over literal McpServerStatus-shaped dicts
# --------------------------------------------------------------------------- #
def test_verdict_zero_declared_is_green() -> None:
    result = evaluate([], [_status("plugin:x:y", tools=[_tool("t")])])
    assert result["verdict"] == "green"
    assert result["reasons"] == []


def test_verdict_declared_none_registered_is_red() -> None:
    result = evaluate([_declared("a")], [])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_connected_with_zero_tools_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_failed_propagates_error_into_reasons() -> None:
    result = evaluate(
        [_declared("a")],
        [_status("plugin:x:a", status="failed", tools=[], error="spawn ENOENT: bad-command")],
    )
    assert result["verdict"] == "red"
    assert any("spawn ENOENT" in r for r in result["reasons"])


def test_verdict_needs_auth_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", status="needs-auth", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_pending_at_deadline_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", status="pending", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_scoped_dynamic_name_matches_declared() -> None:
    # plugin:<bundle>:<server> with scope "dynamic" matches declared "probe" by
    # the :-segment suffix rule.
    result = evaluate(
        [_declared("probe")],
        [_status("plugin:mybundle:probe", scope="dynamic", tools=[_tool("word_count")])],
    )
    assert result["verdict"] == "green"
    assert result["reasons"] == []


def test_verdict_scope_missing_name_prefix_fallback_is_green() -> None:
    # scope key intentionally absent -> plugin_owned via the "plugin:" name-prefix
    # fallback (Section 3 rule 1), so the server still counts and the verdict is green.
    own = _status("plugin:mybundle:probe", scope=None, tools=[_tool("word_count")])
    assert "scope" not in own
    result = evaluate([_declared("probe")], [own])
    assert result["verdict"] == "green"


def test_verdict_partial_registration_is_red() -> None:
    # Two declared, only one connected-with-tools -> red (partial coverage).
    result = evaluate(
        [_declared("a"), _declared("b")],
        [_status("plugin:x:a", tools=[_tool("t")])],
    )
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_unmatched_declared_with_ambient_connected_is_red() -> None:
    # F2 hole (load-bearing): a declared own-server that never registered must go
    # RED even when the host has an AMBIENT (scope project/user) connected-with-tools
    # server present. Deletion check: without the own-server filter + declared-
    # anchoring, a globally-aggregate "is anything connected with tools?" rule would
    # count the ambient server and FALSE-GREEN this exact shape -- the #336 slip.
    #
    # The ambient server's NAME collides with the declared server by the
    # :-segment-suffix rule ("something:probe" ends with ":probe"), so declared-
    # anchoring ALONE is not enough: _find_match would match it. Only the scope
    # own-filter (scope=="project" is not plugin-owned) excludes it and keeps this
    # RED. Deletion check: remove the scope own-filter and this test false-greens.
    ambient = _status("something:probe", scope="project", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("probe")], [ambient])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_ambient_server_never_flips_green() -> None:
    own = _status("plugin:x:a", scope="dynamic", tools=[_tool("t")])
    ambient = _status("proj-server", scope="user", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("a")], [own, ambient])
    assert result["verdict"] == "green"


def test_verdict_ambient_server_never_rescues_red() -> None:
    failed_own = _status("plugin:x:a", status="failed", tools=[], error="boom")
    ambient = _status("proj-server", scope="project", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("a")], [failed_own, ambient])
    assert result["verdict"] == "red"


# --------------------------------------------------------------------------- #
# Test 3 -- string-pointer fingerprint (hints, not reasons) + reasons invariant
# --------------------------------------------------------------------------- #
def test_string_pointer_fingerprint_lives_in_hints_not_reasons() -> None:
    # The real loader silently ignores the string-pointer form, so nothing
    # registers; the diagnostic fingerprint must surface in `hints`.
    # Deletion check: remove the hint emission and this fails.
    declared = extract_declared(str(_MCP_RED_POINTER))
    result = evaluate(declared, [])
    assert result["verdict"] == "red"
    assert any("string pointer" in h.lower() for h in result["hints"])
    assert not any("string pointer" in r.lower() for r in result["reasons"])


def test_reasons_nonempty_iff_verdict_not_green_and_green_may_carry_hints() -> None:
    green = evaluate([], [])
    assert green["verdict"] == "green"
    assert green["reasons"] == []

    red = evaluate([_declared("a")], [])
    assert red["verdict"] == "red"
    assert red["reasons"]

    # E8 bare-file-rescued shape: declared as a string pointer but registered
    # anyway (a coexisting bare .mcp.json). Verdict is GREEN, reasons empty, yet
    # the advisory string-pointer fingerprint still appears in hints.
    rescued = evaluate(
        [_declared("probe", form="string_pointer")],
        [_status("plugin:x:probe", scope="dynamic", tools=[_tool("t")])],
    )
    assert rescued["verdict"] == "green"
    assert rescued["reasons"] == []
    assert any("string pointer" in h.lower() for h in rescued["hints"])


# --------------------------------------------------------------------------- #
# Test 4 -- JSON purity + exit codes (module run as a subprocess)
# --------------------------------------------------------------------------- #
def _run_module(
    plugin_dir: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AGENTOS_PLUGIN_DIR": plugin_dir}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agentos_runner.check"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_module_nonexistent_dir_exits_2_with_invalid_bundle_json(tmp_path: Path) -> None:
    proc = _run_module(str(tmp_path / "does-not-exist"), env_extra={"HOME": str(tmp_path)})
    assert proc.returncode == 2
    # stdout must be exactly ONE json.loads-able document (stderr may carry logs).
    doc = json.loads(proc.stdout)
    assert doc["verdict"] == "invalid_bundle"
    assert doc["reasons"]


def test_module_invalid_bundle_exits_2_with_json(tmp_path: Path) -> None:
    bad = _PLUGIN_FORMAT_FIXTURES / "bad_manifest_name"
    proc = _run_module(str(bad), env_extra={"HOME": str(tmp_path)})
    assert proc.returncode == 2
    doc = json.loads(proc.stdout)
    assert doc["verdict"] == "invalid_bundle"
    assert doc["reasons"]


# --------------------------------------------------------------------------- #
# Test 5 -- real-loader integration (gated on the real `claude` CLI, isolated HOME)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not _CLAUDE_ON_PATH,
    reason="requires the real `claude` CLI on PATH to spawn MCP servers",
)
def test_run_check_green_bundle_registers_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolated HOME (plan Section 5, E7): the CLI caches a failed MCP connection
    # ~15 min so consecutive host runs go flaky, and an ambient HOME leaks
    # project/user MCP servers into the status; a clean HOME leaves only the
    # bundle's own servers. No credential env is set (spike: connect() is
    # credential-free and there is no query() in the code path).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = anyio.run(run_check, str(_MCP_GREEN))
    assert result["verdict"] == "green", result
    assert result["reasons"] == []


@pytest.mark.skipif(
    not _CLAUDE_ON_PATH,
    reason="requires the real `claude` CLI on PATH to spawn MCP servers",
)
def test_run_check_red_pointer_bundle_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = anyio.run(run_check, str(_MCP_RED_POINTER))
    assert result["verdict"] == "red", result
    assert result["reasons"]
