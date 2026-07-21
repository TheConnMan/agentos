"""Minimal stand-in for the real aci_protocol.events module.

Only shaped enough for the gate's class-hierarchy scan: a base class and two
subclasses, one direct and one transitive, to prove the fixpoint walk resolves
both. Never imported or executed by the gate -- only parsed.
"""

from collections.abc import Mapping
from types import MappingProxyType

READER_CONTEXT: Mapping[str, bool] = MappingProxyType({"aci_reader": True})


class _AciModel:
    pass


class Widget(_AciModel):
    pass


class Gadget(Widget):
    pass
