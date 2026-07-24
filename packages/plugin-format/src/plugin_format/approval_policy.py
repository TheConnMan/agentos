"""Normalize the manifest's grantable approval-policy gates (#558).

The operator opt-in ``grantableViaPolicy`` marks a gate whose policy-gate
approval MAY mint a one-shot grant for the tool the gate names (its ``gate``
field, MANIFEST-supplied, never model-supplied). ``grantable_routes`` is the
SINGLE normalization shared by the deploy-time validator
(``plugin_format.validate``) and the runtime loader
(``curie_runner.approval.resolve_approval_policy``). Sharing one helper makes
the two paths identical *by construction* -- the #453/#544 lesson that a
validator and a runtime loader normalizing separately can silently disagree and
ship a fail-open.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from .manifest import resolve_manifest
from .models import ApprovalGate, McpConfig, PluginManifest


def grantable_routes(
    gates: list[ApprovalGate],
) -> tuple[dict[str, str], set[str]]:
    """Resolve the grantable ``{route: tool}`` map and the ambiguous routes.

    For each gate with ``grantableViaPolicy`` truthy AND a non-empty stripped
    ``gate`` AND a non-empty stripped ``route``, accumulate ``route`` -> the set
    of tools claiming it (``tool = gate.gate.strip()``, ``route =
    gate.route.strip()``). ``.strip()`` and case-SENSITIVE comparison mirror
    ``load_approval_policy`` so a config that validates green at deploy resolves
    identically at runtime (#453).

    Returns ``(resolved, ambiguous)``:

    - ``resolved`` maps each route whose tool-set holds exactly ONE distinct tool
      to that tool. A route named twice by the SAME tool is a duplicate, not a
      conflict: still one distinct tool, still resolved.
    - ``ambiguous`` is the set of routes claimed by MORE than one distinct tool.
      Such a route is excluded from ``resolved`` (arms no grant) and reported so
      the deploy validator can reject it, rather than validating green while
      arming nothing (the #453 shape).

    Non-grantable gates and gates with a blank ``gate`` or ``route`` are ignored.
    """

    tools_by_route: dict[str, set[str]] = {}
    for gate in gates:
        if not gate.grantableViaPolicy:
            continue
        tool = gate.gate.strip()
        route = gate.route.strip()
        if not tool or not route:
            continue
        tools_by_route.setdefault(route, set()).add(tool)

    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()
    for route, tools in tools_by_route.items():
        if len(tools) == 1:
            resolved[route] = next(iter(tools))
        else:
            ambiguous.add(route)
    return resolved, ambiguous


# --- Operator gate-name normalization (#703): shared by the deploy validator ----
# and the runtime loader, so an operator-supplied gate name and a manifest gate
# name resolve to the SAME effective runtime form by construction (the #453/#544
# validator/runtime-drift lesson).


def effective_tool_prefix(bundle_name: str, server: str) -> str:
    """The live tool-name prefix the SDK plugin-namespacing produces (#703).

    The SAME template ``validate.py`` builds ``expected_prefixes`` from and
    ``effective_operator_gate`` matches against -- ONE definition shared by the
    deploy-time validator and the runtime loader so the prefix format cannot
    drift between them (the #453/#544 lesson).
    """

    return f"mcp__plugin_{bundle_name}_{server}__"


def _longest_matching_server(
    name: str, servers: set[str], prefix_of: Callable[[str], str]
) -> str | None:
    """The declared server whose ``prefix_of(server)`` matches ``name``, or ``None``.

    A server matches when ``name`` starts with ``prefix_of(server)`` AND has a
    non-empty remainder after it (``len(name) > len(prefix)``). When more than
    one declared server matches (one server name is a prefix of another), the
    LONGEST server name wins the tie -- the more specific match.
    """

    best_server: str | None = None
    for s in servers:
        prefix = prefix_of(s)
        if name.startswith(prefix) and len(name) > len(prefix):
            if best_server is None or len(s) > len(best_server):
                best_server = s
    return best_server


def _mcp_server_names(obj: object) -> set[str] | None:
    """The set of server names an mcp declaration object names, or ``None``.

    Accepts both a full config object (``{"mcpServers": {...}}``) and a bare
    servers map (``{name: server}``), matching ``validate._validate_mcp_object``'s
    payload wrapping so the name derivation is identical on both sides. ``None``
    means the declaration failed to validate (unreadable), which poisons the
    declared-server union.
    """

    payload = obj if isinstance(obj, dict) and "mcpServers" in obj else {"mcpServers": obj}
    try:
        config = McpConfig.model_validate(payload)
    except ValidationError:
        return None
    return set(config.mcpServers)


def declared_mcp_server_names(root: str | Path) -> set[str] | None:
    """The MCP server names a bundle declares, or ``None`` when unknowable.

    Reads the manifest's inline ``mcpServers`` object AND the root ``.mcp.json``,
    returning the union of every server name across both. ``None`` is the poison
    value ``validate._validate_mcp`` uses: a declaration existed but could not be
    read (invalid JSON, a config that failed to validate, or the path-string form
    the real loader ignores), so the declared-server set is unknowable and a gate
    cross-check must fail closed rather than assert against a partial set. An empty
    set is the distinct fact that a declaration was read and named no servers.
    """

    root = Path(root)
    servers: set[str] = set()
    unreadable = False

    manifest_path = resolve_manifest(root)
    if manifest_path is not None:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(data)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValidationError):
            # The manifest itself is unreadable, so its declared servers are
            # unknowable -- poison the whole set.
            return None
        declared = manifest.mcpServers
        if isinstance(declared, dict):
            result = _mcp_server_names(declared)
            if result is None:
                unreadable = True
            else:
                servers |= result
        elif isinstance(declared, str):
            # The path-string form parses but the real loader ignores it, so the
            # servers never register: unknowable, not empty.
            unreadable = True

    root_mcp = root / ".mcp.json"
    if root_mcp.is_file():
        try:
            data = json.loads(root_mcp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            unreadable = True
        else:
            result = _mcp_server_names(data)
            if result is None:
                unreadable = True
            else:
                servers |= result

    if unreadable:
        return None
    return servers


def effective_operator_gate(
    bundle_name: str | None, servers: set[str] | None, name: str
) -> str | None:
    """Map an operator gate name to its effective runtime tool name (#703).

    The SDK plugin-prefixes a bundle MCP tool to
    ``mcp__plugin_<bundle>_<server>__<tool>``. An operator who writes the natural
    shorthand ``mcp__<server>__<tool>`` in ``CURIE_APPROVAL_REQUIRED_TOOLS`` must
    have it rewritten to that effective form, or the gate arms a literal that the
    runtime name never matches (a silent fail-open). Returns:

    - the rewritten effective name when ``name`` is ``mcp__<server>__<tool>`` and
      ``<server>`` is a declared bundle server. ``<server>`` is resolved by
      MATCHING the shorthand against the declared servers (a server name may itself
      contain ``__``, so splitting at the first ``__`` would misparse
      ``mcp__foo__bar__do`` as server ``foo``); the longest matching server name
      wins when one is a prefix of another. The effective prefix is built exactly
      as ``validate.py`` constructs ``expected_prefixes``;
    - ``name`` verbatim only for a built-in with no ``mcp__`` prefix (armed by raw
      name), OR for an already ``mcp__plugin_``-prefixed name that MATCHES an
      expected prefix ``mcp__plugin_<bundle>_<server>__`` for a declared server
      (with a non-empty tool remainder), mirroring ``validate.py``'s
      ``expected_prefixes`` check -- an already-prefixed name is NOT trusted blindly
      (a typo'd ``mcp__plugin_wrongbundle_wrongserver__tool`` would arm a literal the
      runtime never matches, a fail-open);
    - ``None`` when ``name`` is ``mcp__``-shaped and cannot be verified against a
      declared server: an undeclared server, no declared servers, ``servers is
      None`` or a falsy ``bundle_name`` (cannot construct the prefix to verify), an
      empty tool remainder, or an already-prefixed name matching no expected
      prefix. The operator override is never deploy-validated, so this runtime
      check is its sole defense -- "cannot verify" fails CLOSED, not through. The
      caller fails closed on ``None``.
    """

    # A built-in tool (Bash, Write, ...) carries no mcp__ prefix: armed by raw
    # name, never rewritten and never server-checked.
    if not name.startswith("mcp__"):
        return name
    # Every mcp__-shaped name is verified against the declared-server set. Without
    # it (unreadable declaration, or no bundle name to build the prefix) nothing can
    # be verified, so fail closed -- this runtime check is the operator override's
    # only defense.
    if servers is None or not bundle_name:
        return None
    # Already the effective plugin-namespaced form: verify it matches an expected
    # prefix mcp__plugin_<bundle>_<server>__ for a declared server (with a non-empty
    # tool remainder), exactly as validate.py asserts. Do NOT trust it verbatim.
    if name.startswith("mcp__plugin_"):
        matched = _longest_matching_server(
            name, servers, lambda s: effective_tool_prefix(bundle_name, s)
        )
        return name if matched is not None else None
    # An mcp__<server>__<tool> shorthand: resolve <server> by matching against the
    # declared servers rather than splitting at the first __ (a server name may
    # contain __). Prefer the longest match to disambiguate when one server name is
    # a prefix of another; require a non-empty tool remainder.
    best_server = _longest_matching_server(name, servers, lambda s: f"mcp__{s}__")
    if best_server is None:
        return None
    tool = name[len(f"mcp__{best_server}__") :]
    return f"mcp__plugin_{bundle_name}_{best_server}__{tool}"
