"""AgentOS worker: concurrency kernel, sandbox substrate, evals.

F1 adds the concurrency kernel: the consumer group over the dispatcher's Valkey
stream, the routing / finish-race / steer / interrupt rules, no-retry-after-
side-effects with human escalation, and crash-recovery reclaim. G1 owns the
``sandbox`` substrate module; K1 will add the eval runner.
"""

from .config import WorkerConfig
from .consumer import Consumer
from .kernel import RETRYABLE_CLASSIFICATIONS, Kernel, TurnOutcome
from .markers import Markers
from .runner_client import RunnerClient, RunnerError, TurnStream
from .slack_sink import AsyncSlackSink, SlackSink
from .threadlock import LockAcquireTimeout, ThreadLock

__version__ = "0.0.0"

__all__ = [
    "RETRYABLE_CLASSIFICATIONS",
    "AsyncSlackSink",
    "Consumer",
    "Kernel",
    "LockAcquireTimeout",
    "Markers",
    "RunnerClient",
    "RunnerError",
    "SlackSink",
    "ThreadLock",
    "TurnOutcome",
    "TurnStream",
    "WorkerConfig",
    "__version__",
]
