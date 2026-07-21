"""Lives under a tests/ directory: proves the gate exempts test files, which
legitimately exercise the strict (no reader context) construction path on
purpose."""

from packages.aci_protocol.src.aci_protocol.events import Widget


def test_strict_construction_rejects_unknown_fields() -> None:
    Widget.model_validate({"unexpected": True})
