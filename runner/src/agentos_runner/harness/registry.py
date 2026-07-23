"""Entry-point discovery for harness contributions (ADR-0060).

Modeled on Databricks' Omnigent (Apache 2.0): a harness registers a Python
entry point under ``ENTRY_POINT_GROUP`` whose value points at a
``get_contribution() -> HarnessContribution`` callable. Two fail-closed guard
rules are ported from Omnigent verbatim, both evaluated on entry-point
*metadata* -- before ``ep.load()`` ever executes a line of the plugin's code,
so an adversarial or merely-broken plugin cannot forge its way past a check by
being expensive or side-effecting to import:

1. **Flat package path refusal.** A registered ``value`` must dot into a real
   package (``agentos_runner.harness.claude:get_contribution``), not a bare
   top-level module (``claude:get_contribution``). A flat path is exactly the
   shape most likely to collide with something else importable on the path,
   and it is a namespacing smell a legitimate harness package never has a
   reason to produce.
2. **Built-in name collision refusal.** A registered *key* that matches a
   name in ``BUILTIN_HARNESS_CANONICAL_PATHS`` is refused unless its ``value``
   is that name's own canonical path. Silently shadowing the Claude harness
   is the single worst failure this registry could produce, so an ambiguous
   registration is refused rather than resolved by load order.

Both refusals are fail-closed by design: an ambiguous or malformed registry
entry is worse than a missing harness.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as _iter_entry_points

from .contribution import HarnessContribution

ENTRY_POINT_GROUP = "agentos.harness"

# Canonical "module:attr" path for each built-in harness's own self-registration.
# A third-party entry point may not claim one of these keys under any other path.
BUILTIN_HARNESS_CANONICAL_PATHS: dict[str, str] = {
    "claude": "agentos_runner.harness.claude:get_contribution",
}

# The harness selected when none is named (``AGENTOS_HARNESS`` unset): the
# built-in Claude harness. Single source shared by the runner config and the
# boot path so the default name is declared exactly once.
DEFAULT_HARNESS = "claude"


class FlatHarnessPackageError(RuntimeError):
    """Raised when a harness entry point registers a flat (unpackaged) module path."""


class HarnessNameCollisionError(RuntimeError):
    """Raised when a harness entry point claims a built-in harness's name."""


class UnknownHarnessError(RuntimeError):
    """Raised when a requested harness name (or alias) is not registered."""


def _check_guards(ep: EntryPoint) -> None:
    module_path = ep.value.split(":", 1)[0]
    if "." not in module_path:
        raise FlatHarnessPackageError(
            f"harness entry point {ep.name!r} registers a flat package path "
            f"{ep.value!r}; a harness must live in a real package, not a "
            "top-level module."
        )
    canonical = BUILTIN_HARNESS_CANONICAL_PATHS.get(ep.name)
    if canonical is not None and ep.value != canonical:
        raise HarnessNameCollisionError(
            f"harness entry point {ep.name!r} claims a built-in harness name "
            f"but registers {ep.value!r}, not the built-in's canonical path "
            f"{canonical!r}."
        )


def discover_contributions(
    *, entry_points: Iterable[EntryPoint] | None = None
) -> dict[str, HarnessContribution]:
    """Discover every registered harness, keyed by its declared name and aliases.

    Guard failures raise rather than silently skipping the offending entry
    point: a malformed or colliding registration is a configuration error the
    operator must fix, not a harness that quietly fails to appear.
    """

    eps = (
        entry_points
        if entry_points is not None
        else _iter_entry_points(group=ENTRY_POINT_GROUP)
    )
    contributions: dict[str, HarnessContribution] = {}
    for ep in eps:
        _check_guards(ep)
        get_contribution = ep.load()
        contribution = get_contribution()
        for key in (contribution.name, *contribution.aliases):
            contributions[key] = contribution
    return contributions


def resolve_harness(
    name: str, *, contributions: dict[str, HarnessContribution] | None = None
) -> HarnessContribution:
    """Resolve a harness by its declared name or one of its aliases."""

    available = contributions if contributions is not None else discover_contributions()
    try:
        return available[name]
    except KeyError:
        raise UnknownHarnessError(
            f"no harness registered under {name!r} (known: {sorted(available)})"
        ) from None
