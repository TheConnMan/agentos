"""AgentOS dispatcher (Slack Bolt, Socket Mode).

Acks Slack events fast, posts an in-thread placeholder, and enqueues a normalized
job onto a Valkey Stream keyed by the Slack event id (idempotent), under
reconnect supervision. ``QueuedSlackEvent`` is the queue seam the worker (F1)
consumes.
"""

from .app import (
    SocketModeConnection,
    build_app,
    build_redis,
    build_web_client,
)
from .config import DispatcherConfig
from .handlers import is_actionable, process_event, register_handlers
from .queue import (
    STREAM_PAYLOAD_FIELD,
    QueuedSlackEvent,
    claim_event,
    enqueue,
)
from .supervisor import BackoffPolicy, Connection, Supervisor

__version__ = "0.0.0"

__all__ = [
    "STREAM_PAYLOAD_FIELD",
    "BackoffPolicy",
    "Connection",
    "DispatcherConfig",
    "QueuedSlackEvent",
    "SocketModeConnection",
    "Supervisor",
    "__version__",
    "build_app",
    "build_redis",
    "build_web_client",
    "claim_event",
    "enqueue",
    "is_actionable",
    "process_event",
    "register_handlers",
]
