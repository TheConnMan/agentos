"""The worker's approval-record client (#244, ADR-0010).

When a run ends ``awaiting-approval`` the kernel persists a durable ``Approval``
record before suspending the session, so the pending human decision survives
every component restarting. The record lives server-side with the API (the
authorizer of #246 is enforced there, where it cannot be spoofed from inside a
sandbox); this module is the thin write client, mirroring the eval lane's
``EvalReporter`` (same base URL + shared API key).

Creation is idempotent: ``dedupe_key`` carries the triggering event id, so a
reclaimed/redelivered turn that re-requests the same approval adopts the
existing record (the API answers 200 instead of 201) rather than forking a
second pending record for one human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel


class ApprovalRequest(BaseModel):
    """The creation payload, matching the API's ApprovalCreate schema."""

    agent_id: UUID | None = None
    conversation_id: str
    author: str
    summary: str
    reply_channel: str
    reply_placeholder: str
    reply_endpoint: str | None = None
    dedupe_key: str
    expires_in_seconds: int | None = None


@dataclass(frozen=True)
class CreatedApproval:
    """What the kernel needs back: the record's identity and its status."""

    id: str
    status: str


class ApprovalBackendError(Exception):
    """The approval record could not be created; the kernel escalates rather
    than suspending a session no resolution could ever wake."""


class ApprovalCreator(Protocol):
    """The kernel-facing seam; tests supply a recording fake."""

    async def create(self, request: ApprovalRequest) -> CreatedApproval: ...


class ApprovalClient:
    """HTTP implementation against the platform API's /approvals endpoint."""

    def __init__(self, *, api_base_url: str, api_key: str, client: httpx.AsyncClient) -> None:
        self._url = f"{api_base_url.rstrip('/')}/approvals"
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client

    async def create(self, request: ApprovalRequest) -> CreatedApproval:
        try:
            response = await self._client.post(
                self._url,
                content=request.model_dump_json(),
                headers={**self._headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApprovalBackendError(f"approval create failed: {exc}") from exc
        # 201 is a fresh record; 200 is the idempotent dedupe_key replay.
        if response.status_code not in (200, 201):
            raise ApprovalBackendError(
                f"approval create failed: HTTP {response.status_code}: {response.text}"
            )
        body = response.json()
        return CreatedApproval(id=str(body["id"]), status=str(body["status"]))
