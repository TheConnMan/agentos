"""Structured Slack replies: a ``Reply`` type, a Block Kit renderer, and a
convention for a plugin to emit one over the plain-text ACI channel.

AgentOS streams the model's answer as text and edits a placeholder in place, so
a reply is normally just markdown. This module lets a plugin opt into a richer
reply -- a header, a two-column field section, chunked body sections, and a
footer -- without changing the frozen ACI event contract: the plugin emits a
fenced block as the last thing in its answer,

    ```agentos-reply
    {"header": "Top leaks", "text": "...", "fields": [["Open", "12"]],
     "footer": "as of today"}
    ```

and the SlackSink renders it as Block Kit. Anything that is not a *complete*,
valid block (including a half-streamed one) falls back to the existing text
path, so streaming partials never show raw JSON and a malformed block degrades
to plain text rather than breaking the reply.

Ported from the CurieTech agent-ss-template ``blocks.py`` (the 3000-char section
cap, mrkdwn normalization, nav-leftmost button ordering); rendering reuses this
package's ``mrkdwn.to_mrkdwn``. Buttons are rendered here but only become
interactive once the dispatcher handles their clicks (a separate change).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .mrkdwn import to_mrkdwn

# Slack hard-caps a section's text at 3000 chars; stay under it with margin.
_CHUNK_TARGET = 2900

_FENCE = re.compile(r"```agentos-reply\s*\n(.*?)\n```", re.DOTALL)
_FENCE_OPEN = "```agentos-reply"


@dataclass
class Reply:
    """A formatted reply, independent of Block Kit. ``text`` is markdown (also
    the plain-text fallback); ``header`` a header block; ``fields`` a two-column
    section; ``buttons`` an actions block of ``(label, action_id)`` pairs;
    ``footer`` a trailing context line."""

    text: str = ""
    header: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)
    buttons: list[tuple[str, str]] = field(default_factory=list)
    footer: str | None = None


def chunk(text: str, limit: int = _CHUNK_TARGET) -> list[str]:
    """Split ``text`` into pieces no longer than ``limit``, preferring line
    boundaries but hard-splitting any single over-long line. Empty -> []."""
    if not text:
        return []
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def _button(label: str, action_id: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "action_id": action_id,
    }


def _is_nav(label: str) -> bool:
    """Back ('<-') / up-to-hub ('^') buttons sort leftmost."""
    return label.lstrip()[:1] in ("←", "↑")


def to_blocks(reply: Reply) -> list[dict[str, Any]]:
    """Render a ``Reply`` to a Block Kit blocks array. Order: header -> fields ->
    body section(s) (chunked) -> buttons (nav-leftmost) -> footer."""
    blocks: list[dict[str, Any]] = []
    if reply.header:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": reply.header}})
    if reply.fields:
        blocks.append(
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in reply.fields],
            }
        )
    for piece in chunk(to_mrkdwn(reply.text)):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": piece}})
    if reply.buttons:
        ordered = sorted(reply.buttons, key=lambda b: 0 if _is_nav(b[0]) else 1)
        blocks.append(
            {"type": "actions", "elements": [_button(label, aid) for label, aid in ordered]}
        )
    if reply.footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": reply.footer}]})
    return blocks


def _pairs(raw: Any) -> list[tuple[str, str]]:
    """Coerce a JSON list of [k, v] pairs to tuples, dropping malformed entries."""
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, list | tuple) and len(item) == 2:
                out.append((str(item[0]), str(item[1])))
    return out


def parse_reply(text: str) -> Reply | None:
    """A ``Reply`` if ``text`` contains a complete, valid ``agentos-reply`` block,
    else None. Defensive: any parse/shape error returns None so the caller falls
    back to plain text rather than surfacing a broken reply."""
    match = _FENCE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    header = data.get("header")
    footer = data.get("footer")
    return Reply(
        text=str(data.get("text", "")),
        header=str(header) if header is not None else None,
        fields=_pairs(data.get("fields")),
        buttons=_pairs(data.get("buttons")),
        footer=str(footer) if footer is not None else None,
    )


def render(text: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Map an outbound reply string to ``(text, blocks)`` for ``chat.update``.

    - A complete ``agentos-reply`` block -> (plain fallback text, blocks).
    - A half-streamed block (fence opened, not closed) -> (text before the fence,
      None), so streaming never shows raw JSON.
    - Anything else -> (mrkdwn text, None), the existing plain path.
    """
    reply = parse_reply(text)
    if reply is not None:
        return (to_mrkdwn(reply.text) or "(reply)", to_blocks(reply))
    open_at = text.find(_FENCE_OPEN)
    if open_at != -1:
        prefix = text[:open_at].strip()
        return (to_mrkdwn(prefix) if prefix else "…", None)
    return (to_mrkdwn(text), None)
