"""Tolerant request-body types for the shared ACI wire models (#492).

The wire is strict producers, tolerant consumers (``packages/CLAUDE.md``): a
consumer decoding the wire ignores fields it does not model, which is what makes
a new optional field a genuine patch. Every other consumer gets that from an
``aci_protocol.parse_*`` helper, which threads the reader context. A FastAPI
route cannot: FastAPI validates the request body itself and has no seam to pass
a validation context, so a bare ``data: ApprovalRequest`` annotation decodes
strictly and 422s a forward-compatible payload from a newer worker.

These aliases restore tolerance at that seam. The ``BeforeValidator`` runs ahead
of the model's own strict-on-construction check and performs the decode itself
with the SAME ``READER_CONTEXT`` the ``parse_*`` helpers use -- not a second
tolerance mechanism -- returning a validated instance the outer schema then
accepts as-is. Two properties this preserves deliberately:

- **The OpenAPI schema is unchanged.** ``BeforeValidator`` does not alter the
  wrapped model's JSON schema, so the committed ``openapi.json`` still describes
  the request body by the model's own name. No duplicated model, which is the
  whole point of #492.
- **A genuinely invalid body is still a 422** with per-field ``loc``: the inner
  ``ValidationError`` propagates out of ``validate_python`` unchanged.

Producers are untouched: they construct these models directly, where an unknown
field is still an error at the source.
"""

from typing import Annotated, Any

from aci_protocol import READER_CONTEXT, ApprovalRequest, EvalReport
from pydantic import BaseModel, BeforeValidator


def _reader_decode[M: BaseModel](model: type[M]) -> Any:
    """Build a before-validator that decodes ``model`` with the reader context."""

    def _decode(value: Any) -> Any:
        # Only a raw mapping is a wire decode. Anything else (an already-built
        # instance) is passed through for the model schema to judge.
        if isinstance(value, dict):
            return model.model_validate(value, context=READER_CONTEXT)
        return value

    return BeforeValidator(_decode)


ApprovalRequestBody = Annotated[ApprovalRequest, _reader_decode(ApprovalRequest)]
EvalReportBody = Annotated[EvalReport, _reader_decode(EvalReport)]

__all__ = ["ApprovalRequestBody", "EvalReportBody"]
