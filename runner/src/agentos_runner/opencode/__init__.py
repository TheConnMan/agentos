"""OpenCode second-harness spike (issue #25).

An additive, opt-in ``ModelSession`` that drives a live ``opencode serve``
subprocess behind the runner's frozen adapter seam. The claude-agent-sdk runner
stays the default; nothing in the runner core changes. See ``session.py`` for the
live adapter, ``synth.py`` for the OpenCode-frame -> SDK-message shim, and
``conformance.py`` for the live ACI conformance entrypoint.
"""

from __future__ import annotations

from .conformance import opencode_conformance_producer
from .installer import CompiledOpenCodeBundle, OpenCodeBundleInstaller
from .session import OPENCODE_READONLY_TOOLS, OpenCodeModelSession
from .synth import TurnSynthesizer

__all__ = [
    "OpenCodeModelSession",
    "OPENCODE_READONLY_TOOLS",
    "CompiledOpenCodeBundle",
    "OpenCodeBundleInstaller",
    "TurnSynthesizer",
    "opencode_conformance_producer",
]
