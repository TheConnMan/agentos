"""The negative control: an unsanctioned decode with no reader context and no
allowlist entry. The gate must flag this exact call site -- if it does not,
the gate is vacuous. Also proves transitive subclass resolution: Gadget is
two levels below _AciModel (Gadget -> Widget -> _AciModel)."""

from packages.aci_protocol.src.aci_protocol.events import Gadget


def decode(raw: dict[str, object]) -> Gadget:
    return Gadget.model_validate(raw)
