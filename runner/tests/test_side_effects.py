"""The side-effect classifier: read-only allowlist, deny-by-default."""

from agentos_runner import DEFAULT_IDEMPOTENT_TOOLS, SideEffectClassifier


def test_read_only_tools_are_idempotent() -> None:
    classifier = SideEffectClassifier()
    for tool in DEFAULT_IDEMPOTENT_TOOLS:
        assert not classifier.is_side_effecting(tool)


def test_mutating_and_unknown_tools_flag() -> None:
    classifier = SideEffectClassifier()
    # Known mutating tools and any unknown/new tool are treated as side-effecting.
    for tool in ("Bash", "Write", "Edit", "SomeBrandNewTool"):
        assert classifier.is_side_effecting(tool)


def test_override_replaces_the_allowlist() -> None:
    classifier = SideEffectClassifier(["Bash"])
    assert not classifier.is_side_effecting("Bash")
    # An override replaces (does not extend) the default set.
    assert classifier.is_side_effecting("Read")
