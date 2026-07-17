"""Secret redaction over runner stdout logs and gen_ai span attribute values (#518).

Defense-in-depth: the runner forwards a model credential and per-connector secrets
into the sandbox, and an exception message or provider error body can echo one back
onto stdout (which the worker captures) or into a span attribute (which ships to
Langfuse). This module is the single source of truth for the credential-shaped
patterns and the one ``redact`` function both the logging filter (``__main__``) and
the span-attribute choke point (``otel``) apply, so the two cannot drift.

It is a values-only output filter: it never adds or renames a log field or a span
attribute key, so it stays compatible with the frozen ACI env contract, the
``check`` CLI's committed JSON shape, and #512's forthcoming typed-telemetry
validator (which drops records with unscrubbed secrets -- this scrub runs first).
"""

from __future__ import annotations

import logging
import re

PLACEHOLDER = "[REDACTED]"

# (name, pattern, sample). ``sample`` is a synthetic token the pattern must match,
# used by the tripwire test so a new pattern added here without being wired into
# ``redact`` -- or a pattern that stops matching its own shape -- fails CI. None of
# the samples is a real secret.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("anthropic-oauth", re.compile(r"sk-ant-oat[A-Za-z0-9_-]{6,}"), "sk-ant-oat01ABCDEFghij"),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{6,}"), "sk-ant-api03ABCDEFghij"),
    ("openrouter", re.compile(r"sk-or-[A-Za-z0-9_-]{6,}"), "sk-or-v1ABCDEFghijkl"),
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-ABCDEFGHIJKLMNOPQRSTUV"),
    ("xai", re.compile(r"\bxai-[A-Za-z0-9]{16,}\b"), "xai-ABCDEFGHIJKLMNOPqrst"),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIAIOSFODNN7EXAMPLE"),
    (
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "xoxb-1234567890-ABCDEFGH"),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{10,}"), "Bearer abcdef0123456789xyz"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
        "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM",
    ),
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "-----BEGIN RSA PRIVATE KEY-----\nMIIABC\n-----END RSA PRIVATE KEY-----",
    ),
    (
        "url-credential-param",
        re.compile(r"(?i)([?&](?:token|api[_-]?key|secret|password|access[_-]?token)=)[^&\s]+"),
        "https://x.example?token=supersecretvalue",
    ),
]


def redact(text: str) -> str:
    """Replace every credential-shaped token in ``text`` with ``PLACEHOLDER``.

    Order matters: the more specific ``sk-ant-oat``/``sk-ant-``/``sk-or-`` shapes
    run before the generic ``sk-`` catch-all so the emitted placeholder is stable.
    A non-secret string passes through unchanged.
    """

    for _name, pattern, _sample in _PATTERNS:
        if _name == "url-credential-param":
            # Keep the identifying key (token=), redact only the value.
            text = pattern.sub(lambda m: m.group(1) + PLACEHOLDER, text)
        else:
            text = pattern.sub(PLACEHOLDER, text)
    return text


class RedactingLogFilter(logging.Filter):
    """A logging filter that scrubs the fully-interpolated message.

    Attached to every root handler, so a secret interpolated into ANY ``logger.*``
    call -- most importantly an exception string echoed via ``%s`` -- is redacted
    before it reaches stdout. It rewrites ``record.msg`` to the redacted, already-
    formatted message and clears ``record.args`` so downstream formatting is a
    no-op on the scrubbed text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad %-format should never crash the log path
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to every handler on the root logger.

    Called once after ``logging.basicConfig``. A filter on the handler (not the
    logger) scrubs records propagated from any child logger too, since they are
    emitted through the root handler.
    """

    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingLogFilter) for f in handler.filters):
            handler.addFilter(RedactingLogFilter())
