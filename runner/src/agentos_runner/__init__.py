"""AgentOS runner: a claude-agent-sdk streaming session server speaking ACI v0.1.

Productizes the PT-2/PT-E prototypes into a long-lived, single-session HTTP server
that implements the frozen ACI contract from ``packages/aci-protocol``: it accepts
inbound event/interrupt frames, streams outbound NDJSON (text_delta / tool_note /
final / error / side_effect_flag) with protocol-version enforcement, honors the
ACI SessionConfig from the environment, enforces the per-run output-token budget,
flags non-idempotent tool calls, loads a validated plugin bundle, exports gen_ai
OTel spans to the collector, and rehydrates from a history ref on start.
"""

from .adapter import (
    CLAUDE_READONLY_TOOLS,
    ClaudeAgentSession,
    ModelSession,
    build_options,
    map_sdk_message,
)
from .budget import BUDGET_CLASSIFICATION, BudgetTracker
from .config import RunnerConfig
from .conformance import conformance_producer
from .events import AssistantText, RateLimit, ToolCall, TurnEvent, TurnResult
from .fake import FakeModelSession
from .otel import RunTracer, build_tracer_provider
from .plugin import BundleInstaller, ClaudeBundleInstaller, PluginBundleError, load_plugins
from .server import create_app
from .session import SessionRunner
from .side_effects import SideEffectClassifier

__version__ = "0.0.0"

__all__ = [
    "ModelSession",
    "ClaudeAgentSession",
    "map_sdk_message",
    "build_options",
    "AssistantText",
    "ToolCall",
    "RateLimit",
    "TurnResult",
    "TurnEvent",
    "BudgetTracker",
    "BUDGET_CLASSIFICATION",
    "RunnerConfig",
    "conformance_producer",
    "FakeModelSession",
    "RunTracer",
    "build_tracer_provider",
    "PluginBundleError",
    "BundleInstaller",
    "ClaudeBundleInstaller",
    "load_plugins",
    "create_app",
    "SessionRunner",
    "SideEffectClassifier",
    "CLAUDE_READONLY_TOOLS",
]
