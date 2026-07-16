"""ast-based symbol resolution.

The resolver parses a cited ``.py`` file with ``ast`` and never imports or
executes it: importing app modules to lint a doc would run module-level code,
need the full dependency graph installed, and could fire side effects. A static
parse is correct here and cannot.

Resolution forms:

- ``name`` matches a top-level function, class, or assignment target (a
  module-level constant), or a name bound by a plain ``from x import name``.
- ``Class.method`` matches a function in the body of the named class; deeper
  nesting (``A.B.c``) walks successive class bodies.
- A star-import (``from x import *``) binds nothing resolvable: a symbol that
  would resolve only via a star-import does NOT resolve.
"""

from __future__ import annotations

import ast
from pathlib import Path


class SymbolSyntaxError(Exception):
    """Raised when the cited file does not parse; a clean lint failure."""


def _parse(file_path: Path) -> ast.Module:
    try:
        return ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except SyntaxError as exc:
        raise SymbolSyntaxError(str(exc)) from exc


class SymbolCache:
    """Per-lint-run cache of parsed ASTs, keyed by absolute file path.

    A doc that cites the same file for several symbols would otherwise re-read
    and re-parse it once per citation. Create one instance fresh at the start
    of each ``lint()`` invocation and thread it through; deliberately not a
    module-level ``functools.lru_cache``, which would persist across separate
    lint runs in the same process (e.g. across tests) and could serve a stale
    parse after a file on disk changes between runs.
    """

    def __init__(self) -> None:
        self._parsed: dict[Path, ast.Module] = {}

    def parse(self, file_path: Path) -> ast.Module:
        cached = self._parsed.get(file_path)
        if cached is not None:
            return cached
        tree = _parse(file_path)
        self._parsed[file_path] = tree
        return tree


def resolve_symbol(file_path: Path, dotted: str, cache: SymbolCache | None = None) -> bool:
    """Return True if ``dotted`` resolves in ``file_path`` by static parse.

    Raises ``SymbolSyntaxError`` if the file cannot be parsed, so the caller
    reports a clean finding rather than crashing. When ``cache`` is given, a
    repeated citation of the same file within one lint run reuses the parse
    instead of re-reading and re-parsing the file.
    """
    tree = cache.parse(file_path) if cache is not None else _parse(file_path)
    parts = dotted.split(".")
    return _resolve_in_body(tree.body, parts)


def _resolve_in_body(body: list[ast.stmt], parts: list[str]) -> bool:
    head, rest = parts[0], parts[1:]

    if not rest:
        return _name_bound_in_body(body, head)

    # A dotted remainder means head must name a class we can descend into.
    for node in body:
        if isinstance(node, ast.ClassDef) and node.name == head:
            return _resolve_in_body(node.body, rest)
    return False


def _name_bound_in_body(body: list[ast.stmt], name: str) -> bool:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(_target_binds(target, name) for target in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if _target_binds(node.target, name):
                return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue  # star-import binds nothing resolvable
                bound = alias.asname or alias.name
                if bound == name:
                    return True
    return False


def _target_binds(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Tuple | ast.List):
        return any(_target_binds(elt, name) for elt in target.elts)
    return False
