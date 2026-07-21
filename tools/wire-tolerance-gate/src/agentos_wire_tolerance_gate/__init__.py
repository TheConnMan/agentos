"""Static gate: every ``model_validate``/``model_validate_json``/
``model_validate_strings`` call on an ``_AciModel`` subclass must either thread
the reader-context flag explicitly or be named in the committed allowlist
(issue #625, following #492 and the four independently-caught call sites it
shipped there).

``_AciModel`` (``packages/aci-protocol/src/aci_protocol/events.py``) rejects
unknown keys on construction UNLESS the caller passes
``context={"aci_reader": True}`` (the ``READER_CONTEXT`` mapping exported
alongside it). A consumer decoding an untrusted wire/queue/env payload that
forgets to thread that context gets a strict rejection instead of the intended
tolerant decode -- exactly the #492 failure mode ("a forgotten flag silently
422ing... on a rolling deploy"), which shipped at four call sites before four
independent reviewers caught it by hand on that PR's review.

This gate makes the mistake fail CI instead of waiting for a fifth reviewer:
every direct ``SomeAciModel.model_validate*(...)`` call site across the repo
must either pass ``context=READER_CONTEXT`` (matched syntactically: the call's
``context=`` keyword must mention the reader-context name or its underlying
key), or be declared in ``allowlist.json`` with a reason a human can audit --
mirroring the shape of ``cli/api-mirrors.json``'s per-omission ``why``.

Test files are exempt (any path with a ``tests`` directory component, or a
``test_*.py``/``*_test.py`` filename): several intentionally exercise BOTH the
strict and the tolerant path on purpose (see
``packages/aci-protocol/tests/test_turn.py``, which asserts construction
raises on an unknown field AND that ``parse_queued_turn`` tolerates it), so
forcing a context flag onto a test asserting strict rejection would defeat the
test it is written to be.

This is a syntactic, not semantic, gate: it only resolves literal
``ClassName.model_validate(...)`` call sites where ``ClassName`` is one of the
statically-known ``_AciModel`` subclasses (computed by parsing class
definitions under the frozen ``packages/aci-protocol`` package, never by
importing/executing it). A call through a variable or parameter
(``model.model_validate(...)``, ``cls.model_validate(...)``) is out of its
reach by construction -- ``apps/api/src/agentos_api/wirebody.py`` uses exactly
that shape deliberately (the FastAPI request-body seam that already threads
``READER_CONTEXT`` from a shared generic helper, itself the fix for #492), so
this is a known, accepted limitation rather than a gap discovered later:
widening the gate to trace indirect call sites is real follow-up work, not
required for this gate to do its job on the direct-call-site pattern #492
actually repeated.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AllowlistEntry",
    "CallSite",
    "find_violations",
    "main",
]

_ACI_MODEL_BASE = "_AciModel"
_ACI_PROTOCOL_SRC_REL = "packages/aci-protocol/src/aci_protocol"
_DEFAULT_ALLOWLIST_REL = "tools/wire-tolerance-gate/allowlist.json"

_VALIDATE_METHODS = ("model_validate", "model_validate_json", "model_validate_strings")

# Any of these appearing in the unparsed ``context=`` keyword value is treated
# as "this call threads the reader context": the exported sentinel name, its
# underlying private key, and the wire key string itself, so
# ``context=READER_CONTEXT`` and ``context={_READER_CONTEXT_KEY: True}`` (both
# forms used in this repo today) are both recognized without evaluating the
# expression.
_READER_CONTEXT_MARKERS = ("READER_CONTEXT", "_READER_CONTEXT_KEY", "aci_reader")

# Directories never scanned: dependency/build/cache noise and generated code,
# never first-party source a human wrote.
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "generated",
    }
)


@dataclass(frozen=True)
class CallSite:
    """One ``ClassName.model_validate*(...)`` call site on a known subclass."""

    path: str  # repo-relative, POSIX separators
    line: int
    class_name: str
    method: str

    @property
    def symbol(self) -> str:
        return f"{self.class_name}.{self.method}"


@dataclass(frozen=True)
class AllowlistEntry:
    path: str
    symbol: str
    why: str


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return files


def _is_test_path(rel: str) -> bool:
    rel_path = Path(rel)
    if "tests" in rel_path.parts:
        return True
    name = rel_path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _aci_model_subclasses(repo_root: Path, aci_protocol_src_rel: str) -> set[str]:
    """Every class name transitively subclassing ``_AciModel``.

    Computed by parsing ``class X(Y):`` headers under the frozen aci-protocol
    package's source tree, never by importing it -- the hierarchy is small,
    closed, and lives only in that one package (see ``packages/CLAUDE.md``), so
    a name-based fixpoint over its own files is exact for this repo today. A
    base outside a ``Name`` node (an aliased or dotted import) is not modeled;
    aci-protocol declares every base as a bare name today.
    """

    known = {_ACI_MODEL_BASE}
    src_root = repo_root / aci_protocol_src_rel
    if not src_root.is_dir():
        return known

    bases_by_class: dict[str, set[str]] = {}
    for path in _iter_python_files(src_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                base_names = {b.id for b in node.bases if isinstance(b, ast.Name)}
                bases_by_class.setdefault(node.name, set()).update(base_names)

    changed = True
    while changed:
        changed = False
        for cls, bases in bases_by_class.items():
            if cls not in known and bases & known:
                known.add(cls)
                changed = True

    return known


def _match_call(call: ast.Call, known_classes: set[str]) -> tuple[str, str] | None:
    """Return ``(class_name, method)`` if ``call`` is a direct
    ``ClassName.model_validate*(...)`` call on a known subclass, else ``None``."""

    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _VALIDATE_METHODS:
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id not in known_classes:
        return None
    return func.value.id, func.attr


def _threads_reader_context(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "context":
            continue
        try:
            rendered = ast.unparse(kw.value)
        except (ValueError, TypeError):
            rendered = ""
        if any(marker in rendered for marker in _READER_CONTEXT_MARKERS):
            return True
    return False


def _find_call_sites(repo_root: Path, known_classes: set[str]) -> list[CallSite]:
    sites: list[CallSite] = []
    for path in _iter_python_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        if _is_test_path(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            match = _match_call(node, known_classes)
            if match is None or _threads_reader_context(node):
                continue
            class_name, method = match
            sites.append(CallSite(rel, node.lineno, class_name, method))
    return sites


def _load_allowlist(allowlist_path: Path) -> list[AllowlistEntry]:
    if not allowlist_path.is_file():
        return []
    data = json.loads(allowlist_path.read_text(encoding="utf-8"))
    return [
        AllowlistEntry(path=raw["path"], symbol=raw["symbol"], why=raw["why"])
        for raw in data.get("entries", [])
    ]


def _is_allowlisted(site: CallSite, entries: list[AllowlistEntry]) -> bool:
    return any(entry.path == site.path and entry.symbol == site.symbol for entry in entries)


def find_violations(repo_root: Path, allowlist_path: Path | None = None) -> list[CallSite]:
    """Every non-test ``model_validate*`` call site on an ``_AciModel``
    subclass that neither threads ``READER_CONTEXT`` nor is declared in the
    allowlist. An empty result is the passing case."""

    known_classes = _aci_model_subclasses(repo_root, _ACI_PROTOCOL_SRC_REL)
    sites = _find_call_sites(repo_root, known_classes)
    resolved_allowlist = allowlist_path or (repo_root / _DEFAULT_ALLOWLIST_REL)
    entries = _load_allowlist(resolved_allowlist)
    return [site for site in sites if not _is_allowlisted(site, entries)]


def _git_top_level() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentos_wire_tolerance_gate")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to scan; defaults to the git top-level.",
    )
    parser.add_argument(
        "--allowlist",
        default=None,
        help="Path to the allowlist JSON; defaults to tools/wire-tolerance-gate/allowlist.json.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else _git_top_level()
    allowlist_path = Path(args.allowlist).resolve() if args.allowlist else None

    violations = find_violations(repo_root, allowlist_path)
    for site in violations:
        print(
            f"{site.path}:{site.line}: {site.symbol}(...) decodes an _AciModel "
            "subclass without threading READER_CONTEXT, and is not declared in "
            f"{_DEFAULT_ALLOWLIST_REL} (issue #625). Either pass "
            "context=READER_CONTEXT (see aci_protocol.ndjson.parse_queued_turn "
            "for the sanctioned shape) or add an allowlist entry naming why this "
            "call site is a sanctioned exception."
        )

    if violations:
        print(f"wire-tolerance-gate: {len(violations)} violation(s)")
    else:
        print(
            "wire-tolerance-gate: OK, every model_validate*/model_validate_json "
            "call on an _AciModel subclass is tolerant or allowlisted"
        )

    return 0 if not violations else 1
