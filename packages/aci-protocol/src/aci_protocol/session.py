"""Session setup: the typed SessionConfig and its env-var (de)serialization.

Mirrors the ACI contract v0.1 SESSION SETUP block (section 0). A session is
configured through environment variables and mounted files; SessionConfig is the
typed view of those variables, with helpers to render them to and parse them
from a process environment.

Env mapping:
    AGENTOS_PLUGIN_DIR     -> plugin_dir
    AGENTOS_MEMORY_REF     -> memory_ref        (optional)
    AGENTOS_CREDENTIALS    -> credentials_ref   (optional)
    AGENTOS_SESSION_ID     -> session_id
    AGENTOS_SANDBOX_ID     -> sandbox_id
    AGENTOS_BUDGET         -> budget            (JSON object)
    OTEL_EXPORTER_OTLP_*   -> otel              (endpoint / headers / protocol)
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class Budget(BaseModel):
    """Per-agent budget spec, carried as JSON in AGENTOS_BUDGET.

    ``task_budget_hint`` is the optional hint passed through to the model so it
    self paces (section 6b); it is not a hard ceiling.
    """

    model_config = _STRICT

    max_output_tokens_per_run: int
    task_budget_hint: int | None = None
    max_usd_per_day: float


class OtelConfig(BaseModel):
    """The OTEL_EXPORTER_OTLP_* subset the runner needs to export traces.

    Section 0 lists OTEL_EXPORTER_OTLP_* as a wildcard. We capture the standard
    fields the prototype used (endpoint, headers, protocol); any others pass
    through as raw env vars untouched and are out of scope for this typed view.
    """

    model_config = _STRICT

    endpoint: str | None = None
    headers: str | None = None
    protocol: str | None = None


class SessionConfig(BaseModel):
    """The typed session setup contract.

    ``credentials_ref`` is a reference to injected secrets (AGENTOS_CREDENTIALS);
    section 0 describes these as per-tool secrets via K8s Secret refs, so the
    contract carries the reference, not the secret material itself.
    """

    model_config = _STRICT

    plugin_dir: str
    session_id: str
    sandbox_id: str
    budget: Budget
    memory_ref: str | None = None
    credentials_ref: str | None = None
    otel: OtelConfig = Field(default_factory=OtelConfig)

    def to_env(self) -> dict[str, str]:
        """Render this config to the process environment variables it maps to.

        Optional fields that are unset are omitted rather than emitted empty.
        """

        env: dict[str, str] = {
            "AGENTOS_PLUGIN_DIR": self.plugin_dir,
            "AGENTOS_SESSION_ID": self.session_id,
            "AGENTOS_SANDBOX_ID": self.sandbox_id,
            "AGENTOS_BUDGET": self.budget.model_dump_json(),
        }
        if self.memory_ref is not None:
            env["AGENTOS_MEMORY_REF"] = self.memory_ref
        if self.credentials_ref is not None:
            env["AGENTOS_CREDENTIALS"] = self.credentials_ref
        if self.otel.endpoint is not None:
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.otel.endpoint
        if self.otel.headers is not None:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = self.otel.headers
        if self.otel.protocol is not None:
            env["OTEL_EXPORTER_OTLP_PROTOCOL"] = self.otel.protocol
        return env

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SessionConfig":
        """Parse a SessionConfig from a process environment mapping.

        Missing required variables and a malformed AGENTOS_BUDGET raise the
        usual pydantic ValidationError via the model constructor.
        """

        otel = OtelConfig(
            endpoint=env.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            headers=env.get("OTEL_EXPORTER_OTLP_HEADERS"),
            protocol=env.get("OTEL_EXPORTER_OTLP_PROTOCOL"),
        )
        budget = Budget.model_validate_json(env["AGENTOS_BUDGET"])
        return cls(
            plugin_dir=env["AGENTOS_PLUGIN_DIR"],
            session_id=env["AGENTOS_SESSION_ID"],
            sandbox_id=env["AGENTOS_SANDBOX_ID"],
            budget=budget,
            memory_ref=env.get("AGENTOS_MEMORY_REF"),
            credentials_ref=env.get("AGENTOS_CREDENTIALS"),
            otel=otel,
        )
