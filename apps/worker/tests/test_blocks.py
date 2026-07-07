"""Structured-reply rendering + the agentos-reply parsing convention (pure)."""

from __future__ import annotations

from agentos_worker.blocks import Reply, chunk, parse_reply, render, to_blocks

_BLOCK = """Here you go:

```agentos-reply
{"header": "Top leaks", "text": "**3** open", "fields": [["Open", "3"]],
 "buttons": [["Details", "details"], ["← Back", "home"]], "footer": "now"}
```"""


def _types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


def test_to_blocks_orders_and_renders_parts() -> None:
    reply = Reply(
        text="body",
        header="Title",
        fields=[("A", "1")],
        buttons=[("Go", "go")],
        footer="foot",
    )
    assert _types(to_blocks(reply)) == ["header", "section", "section", "actions", "context"]


def test_to_blocks_sorts_nav_buttons_leftmost() -> None:
    reply = Reply(text="x", buttons=[("Details", "details"), ("← Back", "home")])
    actions = next(b for b in to_blocks(reply) if b["type"] == "actions")
    labels = [e["text"]["text"] for e in actions["elements"]]
    assert labels == ["← Back", "Details"]  # nav sorted to the front


def test_chunk_splits_long_text_under_limit() -> None:
    pieces = chunk("x" * 7000, limit=2900)
    assert all(len(p) <= 2900 for p in pieces)
    assert "".join(pieces) == "x" * 7000


def test_parse_reply_extracts_a_complete_block() -> None:
    reply = parse_reply(_BLOCK)
    assert reply is not None
    assert reply.header == "Top leaks"
    assert reply.fields == [("Open", "3")]
    assert ("← Back", "home") in reply.buttons


def test_parse_reply_none_without_a_block() -> None:
    assert parse_reply("just a normal answer") is None


def test_parse_reply_defensive_on_bad_json() -> None:
    assert parse_reply("```agentos-reply\n{not json}\n```") is None


def test_render_complete_block_returns_blocks() -> None:
    text, blocks = render(_BLOCK)
    assert blocks is not None
    assert _types(blocks)[0] == "header"
    assert "raw" not in text.lower()  # fallback text is the reply body, not JSON


def test_render_hides_half_streamed_block() -> None:
    # Fence opened mid-stream, not yet closed: never show the raw JSON.
    partial = "Working...\n```agentos-reply\n{\"header\": \"T"
    text, blocks = render(partial)
    assert blocks is None
    assert "agentos-reply" not in text
    assert text.startswith("Working")


def test_render_plain_text_is_mrkdwn() -> None:
    text, blocks = render("**hi** [x](http://y)")
    assert blocks is None
    assert text == "*hi* <http://y|x>"
