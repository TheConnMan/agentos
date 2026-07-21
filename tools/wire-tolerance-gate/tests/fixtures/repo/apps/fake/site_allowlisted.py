"""A call site with no reader context, but declared in the fixture allowlist
(see ../../allowlist.json). Proves an allowlisted site is excused."""

from packages.aci_protocol.src.aci_protocol.events import Widget


def decode(raw: dict[str, object]) -> Widget:
    return Widget.model_validate(raw)
