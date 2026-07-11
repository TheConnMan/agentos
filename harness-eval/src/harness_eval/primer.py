"""Fetch the AgentOS primer (``agentos guide``) and wrap it as a prompt preamble.

``subprocess`` is referenced at module scope so tests can monkeypatch
``harness_eval.primer.subprocess.run`` without a real ``agentos`` binary.
"""

from __future__ import annotations

import subprocess


class PrimerUnavailable(Exception):
    """Raised when the ``agentos guide`` primer cannot be fetched."""


def fetch_primer(agentos_bin: str = "agentos") -> str:
    """Return the stdout of ``<agentos_bin> guide``.

    Raises ``PrimerUnavailable`` on a non-zero exit, a missing binary, or a
    timeout so a hung ``agentos guide`` cannot hang the whole run.
    """
    try:
        result = subprocess.run(
            [agentos_bin, "guide"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise PrimerUnavailable(f"{agentos_bin} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrimerUnavailable(f"{agentos_bin} guide timed out") from exc
    if result.returncode != 0:
        raise PrimerUnavailable(
            f"{agentos_bin} guide exited {result.returncode}: {result.stderr}"
        )
    return result.stdout


def primer_prompt_prefix(primer_text: str) -> str:
    """Wrap the primer as an instruction preamble embedding it verbatim."""
    return (
        "You are working inside an AgentOS bundle. Read the project primer below "
        "before you act, and follow its conventions exactly.\n\n"
        "--- BEGIN AGENTOS PRIMER ---\n"
        f"{primer_text}\n"
        "--- END AGENTOS PRIMER ---\n\n"
        "Now complete the following task:"
    )
