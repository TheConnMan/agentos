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

from .behaviorpacks import BehaviorPacks, NavPack, ensure_hub_button
from .mrkdwn import to_mrkdwn

# Slack hard-caps a section's text at 3000 chars; stay under it with margin.
_CHUNK_TARGET = 2900

_FENCE = re.compile(r"```agentos-reply\s*\n(.*?)\n```", re.DOTALL)
_FENCE_OPEN = "```agentos-reply"


@dataclass
class Reply:
    """A formatted reply, independent of Block Kit. ``text`` is markdown (also
    the plain-text fallback); ``status`` a leading context line (rendered
    as-authored, like ``footer``); ``header`` a header block; ``fields`` a
    two-column section; ``buttons`` an actions block of ``(label, action_id)``
    pairs; ``links`` an actions block of ``(label, url)`` URL buttons; ``footer``
    a trailing context line."""

    text: str = ""
    status: str | None = None
    header: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)
    buttons: list[tuple[str, str]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
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


def _button_shell(label: str) -> dict[str, Any]:
    """The shared button element; callers add either ``action_id`` (interactive,
    dispatcher-handled) or ``url`` (a navigational link button)."""
    return {"type": "button", "text": {"type": "plain_text", "text": label, "emoji": True}}


def _button(label: str, action_id: str) -> dict[str, Any]:
    return {**_button_shell(label), "action_id": action_id}


def _context_block(text: str) -> dict[str, Any]:
    """A context block (small greyed line) rendering ``text`` as-authored mrkdwn.
    Used for the leading ``status`` line and the trailing ``footer``."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _is_nav(label: str) -> bool:
    """Back ('<-') / up-to-hub ('^') buttons sort leftmost."""
    return label.lstrip()[:1] in ("←", "↑")


def _is_http_url(url: str) -> bool:
    """A link button's url must be an absolute http(s) URL; Slack rejects the
    whole message otherwise. Anything else (empty, relative, other scheme) is
    dropped so one bad link never breaks the reply."""
    u = url.strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def _link_button(label: str, url: str) -> dict[str, Any]:
    """A URL button: interactive without the dispatcher (carries ``url``, no
    ``action_id``)."""
    return {**_button_shell(label), "url": url}


def to_blocks(reply: Reply, nav: NavPack | None = None) -> list[dict[str, Any]]:
    """Render a ``Reply`` to a Block Kit blocks array. Order: status (context) ->
    header -> fields -> body section(s) (chunked) -> buttons (nav-leftmost) ->
    links (URL buttons) -> footer.

    ``nav`` is the agent's no-dead-ends hub button: when present and enabled, the
    hub button is appended to the reply's buttons (via ``ensure_hub_button``, the
    platform-owned append policy) before the actions block is built. When ``nav``
    is None or disabled the output is byte-identical to no-nav; ``ensure_hub_button``
    also no-ops when a button already links to the hub."""
    blocks: list[dict[str, Any]] = []
    if reply.status:
        blocks.append(_context_block(reply.status))
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
    # ``reply.buttons`` are already (label, action_id) pairs, the same (label,
    # command) shape ensure_hub_button operates on, so no conversion is needed.
    buttons = (
        reply.buttons if nav is None else ensure_hub_button(BehaviorPacks(nav=nav), reply.buttons)
    )
    if buttons:
        ordered = sorted(buttons, key=lambda b: 0 if _is_nav(b[0]) else 1)
        blocks.append(
            {"type": "actions", "elements": [_button(label, aid) for label, aid in ordered]}
        )
    valid_links = [(label, url) for label, url in reply.links if _is_http_url(url)]
    if valid_links:
        blocks.append(
            {
                "type": "actions",
                "elements": [_link_button(label, url) for label, url in valid_links],
            }
        )
    if reply.footer:
        blocks.append(_context_block(reply.footer))
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
    status = data.get("status")
    header = data.get("header")
    footer = data.get("footer")
    return Reply(
        text=str(data.get("text", "")),
        status=str(status) if status is not None else None,
        header=str(header) if header is not None else None,
        fields=_pairs(data.get("fields")),
        buttons=_pairs(data.get("buttons")),
        links=_pairs(data.get("links")),
        footer=str(footer) if footer is not None else None,
    )


def render(text: str, nav: NavPack | None = None) -> tuple[str, list[dict[str, Any]] | None]:
    """Map an outbound reply string to ``(text, blocks)`` for ``chat.update``.

    - A complete ``agentos-reply`` block -> (plain fallback text, blocks).
    - A half-streamed block (fence opened, not closed) -> (text before the fence,
      None), so streaming never shows raw JSON.
    - Anything else -> (mrkdwn text, None), the existing plain path.

    ``nav`` (the agent's hub-button pack, threaded from the kernel) is applied to
    a complete structured reply's buttons; None/disabled leaves output unchanged.
    """
    reply = parse_reply(text)
    if reply is not None:
        return (to_mrkdwn(reply.text) or "(reply)", to_blocks(reply, nav))
    open_at = text.find(_FENCE_OPEN)
    if open_at != -1:
        prefix = text[:open_at].strip()
        return (to_mrkdwn(prefix) if prefix else "…", None)
    return (to_mrkdwn(text), None)
