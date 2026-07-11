"""The side-effect classifier: read-only allowlist, deny-by-default.

The read-only set is declared per harness adapter, so the classifier is always
constructed with an explicit tool set: ``CLAUDE_READONLY_TOOLS`` for the
claude-agent-sdk path, ``OPENCODE_READONLY_TOOLS`` for the OpenCode path.
"""

from agentos_runner import CLAUDE_READONLY_TOOLS, SideEffectClassifier
from agentos_runner.opencode import OPENCODE_READONLY_TOOLS


def test_read_only_tools_are_idempotent() -> None:
    classifier = SideEffectClassifier(CLAUDE_READONLY_TOOLS)
    for tool in CLAUDE_READONLY_TOOLS:
        assert not classifier.is_side_effecting(tool)


def test_mutating_and_unknown_tools_flag() -> None:
    classifier = SideEffectClassifier(CLAUDE_READONLY_TOOLS)
    # Known mutating tools and any unknown/new tool are treated as side-effecting.
    for tool in ("Bash", "Write", "Edit", "SomeBrandNewTool"):
        assert classifier.is_side_effecting(tool)


def test_override_replaces_the_allowlist() -> None:
    classifier = SideEffectClassifier(["Bash"])
    assert not classifier.is_side_effecting("Bash")
    # An override replaces (does not extend) the declared set.
    assert classifier.is_side_effecting("Read")


def test_claude_declaration_does_not_absorb_opencode_names() -> None:
    # The Claude declaration is PascalCase; OpenCode's lowercase read-only names
    # are NOT in it, so classifying an OpenCode call against the Claude set flags.
    classifier = SideEffectClassifier(CLAUDE_READONLY_TOOLS)
    assert classifier.is_side_effecting("read")


def test_opencode_read_only_tools_are_idempotent() -> None:
    # Regression (issue #308): OpenCode's lowercase read-only built-ins (verified
    # against opencode 1.17.17) must not be misclassified as side-effecting when
    # the OpenCode declaration is used.
    classifier = SideEffectClassifier(OPENCODE_READONLY_TOOLS)
    for tool in ("read", "grep", "glob", "webfetch", "skill"):
        assert not classifier.is_side_effecting(tool)


def test_opencode_mutating_and_unknown_tools_flag() -> None:
    classifier = SideEffectClassifier(OPENCODE_READONLY_TOOLS)
    # ``bash`` is deliberately NOT allowlisted; deny-by-default covers unknowns.
    # ``todoread`` and ``list`` are not real opencode 1.17.17 builtins; keeping
    # them out of the allowlist keeps a fail-open regression from returning.
    for tool in ("bash", "write", "SomeBrandNewTool", "todoread", "list"):
        assert classifier.is_side_effecting(tool)


def test_empty_override_denies_everything() -> None:
    # An operator env of ``AGENTOS_IDEMPOTENT_TOOLS=","`` parses to ``[]`` (not
    # None), and ``__main__.py`` uses ``is not None`` (not ``or``) so that ``[]``
    # reaches the classifier and denies every tool. An empty allowlist must flag
    # even a canonical read.
    classifier = SideEffectClassifier([])
    assert classifier.is_side_effecting("Read")
