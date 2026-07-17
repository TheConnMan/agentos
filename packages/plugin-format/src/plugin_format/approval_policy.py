"""Normalize the manifest's grantable approval-policy gates (#558).

The operator opt-in ``grantableViaPolicy`` marks a gate whose policy-gate
approval MAY mint a one-shot grant for the tool the gate names (its ``gate``
field, MANIFEST-supplied, never model-supplied). ``grantable_routes`` is the
SINGLE normalization shared by the deploy-time validator
(``plugin_format.validate``) and the runtime loader
(``agentos_runner.approval.resolve_approval_policy``). Sharing one helper makes
the two paths identical *by construction* -- the #453/#544 lesson that a
validator and a runtime loader normalizing separately can silently disagree and
ship a fail-open.
"""

from __future__ import annotations

from .models import ApprovalGate


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
