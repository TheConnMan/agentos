"""Pure-unit pin: the worker's permission-gate summary prefix literal must equal
the runner's source constant (#430).

The worker deliberately does NOT import the runner package at runtime, so it
duplicates the ``APPROVAL_SUMMARY_PREFIX`` literal as
``_PERMISSION_GATE_SUMMARY_PREFIX`` to recover the approved tool name from a
durable approval summary. If the two ever diverge, the grant silently stops
matching and every approved permission-gate action fails to complete.

This runs with NO Postgres and NO fixtures: both constants are imported at module
import time, so the pin is checked in EVERY test lane, not only when the compose
DB is up (the previous pin lived inside a Postgres-gated integration test).
"""

from __future__ import annotations

from curie_runner.approval import APPROVAL_SUMMARY_PREFIX
from curie_worker.binding import _PERMISSION_GATE_SUMMARY_PREFIX


def test_worker_prefix_matches_runner_source() -> None:
    assert _PERMISSION_GATE_SUMMARY_PREFIX == APPROVAL_SUMMARY_PREFIX
