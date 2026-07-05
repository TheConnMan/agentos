"""The ACI protocol version constant.

This is the single source of truth for the wire version embedded in every
outbound event and in the exported JSON Schema. The frozen interface rule
(see the package README) requires bumping this whenever any model changes,
and regenerating the committed schemas and generated types in the same commit.

``ProtocolVersionLiteral`` types the ``version`` field on outbound events so the
exact value is enforced by the models, the JSON Schema (a ``const``), and the
generated TypeScript, not just by the NDJSON decoder. Keep the two in lockstep;
``tests/test_events.py`` asserts the literal equals PROTOCOL_VERSION.
"""

from typing import Final, Literal

PROTOCOL_VERSION: Final = "0.1.0"

ProtocolVersionLiteral = Literal["0.1.0"]
