"""Pydantic v2 request/response models for the API surface."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import Environment


class AgentCreate(BaseModel):
    name: str
    slack_channel: str


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slack_channel: str
    created_at: datetime


class VersionCreate(BaseModel):
    version_label: str
    bundle_ref: str | None = None
    created_by: str


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_label: str
    bundle_ref: str | None
    bundle_sha256: str | None
    created_by: str
    created_at: datetime


class BundleOut(BaseModel):
    """Result of storing a bundle for a version."""

    version_id: uuid.UUID
    bundle_ref: str
    bundle_sha256: str
    size_bytes: int


class BundleValidationError(BaseModel):
    """Returned (HTTP 422) when a bundle fails plugin-format validation."""

    detail: str = "bundle failed validation"
    errors: list[dict[str, str]]


class DeploymentCreate(BaseModel):
    agent_id: uuid.UUID
    version_id: uuid.UUID
    environment: Environment
    status: str = "active"


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_id: uuid.UUID
    environment: Environment
    status: str
    deployed_at: datetime


class ObservationNode(BaseModel):
    """One node in the reconstructed observation tree (Runs view)."""

    id: str
    type: str
    name: str | None = None
    startTime: str | None = None  # noqa: N815 (Langfuse wire field name)
    model: str | None = None
    usageDetails: dict[str, Any] | None = None  # noqa: N815
    children: list["ObservationNode"] = []


class TraceTree(BaseModel):
    """A trace plus its reconstructed observation tree."""

    trace: dict[str, Any]
    tree: list[ObservationNode]
