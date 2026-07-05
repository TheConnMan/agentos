"""The ACI protocol version constant.

This is the single source of truth for the wire version embedded in every
outbound event and in the exported JSON Schema. The frozen interface rule
(see the package README) requires bumping this whenever any model changes,
and regenerating the committed schemas and generated types in the same commit.
"""

PROTOCOL_VERSION = "0.1.0"
