"""Structured-reply rendering + the agentos-reply parsing convention (pure)."""

from __future__ import annotations

from agentos_worker.behaviorpacks import NavPack
from agentos_worker.blocks import Reply, chunk, parse_reply, render, to_blocks

# An enabled nav pack, same shape as tests/test_behaviorpacks.py.
_NAV = NavPack(enabled=True, hub_label="Help", hub_command="help")


def _action_ids(blocks: list[dict]) -> list[str]:
    return [
        e["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
    ]


def _action_labels(blocks: list[dict]) -> list[str]:
    return [
        e["text"]["text"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
    ]

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


# -- nav pack wiring into the render path -------------------------------------
# Contract: to_blocks(reply, nav=None) / render(text, nav=None) gain an optional
# nav: NavPack | None. When nav is enabled, the hub button is appended to the
# actions block before it is emitted; when nav is None/disabled, output is
# byte-identical to today (no hub button).


def test_to_blocks_appends_hub_button_when_nav_enabled() -> None:
    reply = Reply(text="x", buttons=[("Details", "details")])
    blocks = to_blocks(reply, nav=_NAV)
    assert "help" in _action_ids(blocks)  # the hub command reached the actions block
    assert "← Help" in _action_labels(blocks)  # left-arrow: no back button present


def test_to_blocks_nav_none_is_identical_to_no_nav() -> None:
    reply = Reply(text="x", buttons=[("Details", "details")])
    assert to_blocks(reply, nav=None) == to_blocks(reply)
    assert "help" not in _action_ids(to_blocks(reply, nav=None))


def test_to_blocks_disabled_nav_adds_no_hub_button() -> None:
    disabled = NavPack(enabled=False, hub_label="Help", hub_command="help")
    reply = Reply(text="x", buttons=[("Details", "details")])
    assert to_blocks(reply, nav=disabled) == to_blocks(reply)
    assert "help" not in _action_ids(to_blocks(reply, nav=disabled))


def test_render_applies_nav_hub_button() -> None:
    # _BLOCK already carries a "← Back" nav button, so the hub is above -> "↑".
    _text, blocks = render(_BLOCK, nav=_NAV)
    assert blocks is not None
    assert "help" in _action_ids(blocks)
    assert "↑ Help" in _action_labels(blocks)


def test_render_without_nav_has_no_hub_button() -> None:
    _text, blocks = render(_BLOCK)
    assert blocks is not None
    assert "help" not in _action_ids(blocks)


# --- #31: status context + link buttons --------------------------------------


def test_status_renders_as_first_context_block() -> None:
    reply = Reply(text="body", status="Running the numbers")
    blocks = to_blocks(reply)
    first = blocks[0]
    assert first["type"] == "context"
    assert first["elements"] == [{"type": "mrkdwn", "text": "Running the numbers"}]


def test_status_is_rendered_as_authored_not_mrkdwn_converted() -> None:
    # Like footer, status is a context line rendered as-authored (no Markdown ->
    # mrkdwn conversion); a Markdown link stays verbatim rather than <url|label>.
    reply = Reply(text="body", status="**bold** [x](http://y)")
    first = to_blocks(reply)[0]
    assert first["type"] == "context"
    assert first["elements"][0]["text"] == "**bold** [x](http://y)"


def test_parse_reply_extracts_status() -> None:
    reply = parse_reply('```agentos-reply\n{"status": "Working", "text": "b"}\n```')
    assert reply is not None
    assert reply.status == "Working"


def test_parse_reply_status_is_none_without_the_key() -> None:
    reply = parse_reply('```agentos-reply\n{"text": "b"}\n```')
    assert reply is not None
    assert reply.status is None


def test_links_render_as_url_buttons() -> None:
    reply = Reply(text="x", links=[("Docs", "https://x/y")])
    actions = [b for b in to_blocks(reply) if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    assert len(elements) == 1
    el = elements[0]
    assert el["url"] == "https://x/y"
    assert el["text"]["text"] == "Docs"
    assert "action_id" not in el  # a URL button is interactive without the dispatcher


def test_links_with_invalid_url_are_dropped() -> None:
    # Only the absolute http(s) link survives; empty / relative / non-URL are dropped
    # so one malformed url never makes Slack reject the whole reply.
    reply = Reply(
        text="x",
        links=[("Good", "https://ok.com"), ("Empty", ""), ("Rel", "/relative"), ("Bad", "None")],
    )
    actions = [b for b in to_blocks(reply) if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    assert len(elements) == 1
    assert elements[0]["url"] == "https://ok.com"
    labels = [e["text"]["text"] for e in elements]
    assert "Empty" not in labels
    assert "Rel" not in labels
    assert "Bad" not in labels


def test_links_all_invalid_emits_no_actions_block() -> None:
    # Every link url is invalid and there are no buttons: no actions block at all.
    reply = Reply(text="x", links=[("Empty", ""), ("Rel", "/relative"), ("Bad", "None")])
    assert not any(b["type"] == "actions" for b in to_blocks(reply))


def test_parse_reply_extracts_links() -> None:
    reply = parse_reply('```agentos-reply\n{"links": [["Docs", "https://x/y"]], "text": "b"}\n```')
    assert reply is not None
    assert reply.links == [("Docs", "https://x/y")]


def test_parse_reply_drops_malformed_links() -> None:
    reply = parse_reply(
        '```agentos-reply\n'
        '{"links": [["only-one"], ["a", "b", "c"], ["Docs", "https://x/y"]], "text": "b"}\n'
        '```'
    )
    assert reply is not None
    assert reply.links == [("Docs", "https://x/y")]  # 1-elem and 3-elem entries dropped


def test_to_blocks_full_order_with_status_and_links() -> None:
    reply = Reply(
        text="body",
        status="Working",
        header="Title",
        fields=[("A", "1")],
        buttons=[("Go", "go")],
        links=[("Docs", "https://x/y")],
        footer="foot",
    )
    blocks = to_blocks(reply)
    assert _types(blocks) == [
        "context",  # status first
        "header",
        "section",  # fields
        "section",  # body
        "actions",  # buttons
        "actions",  # links
        "context",  # footer last
    ]
    actions = [b for b in blocks if b["type"] == "actions"]
    # First actions block is buttons (action_id elements), second is links (url).
    assert all("action_id" in e for e in actions[0]["elements"])
    assert all("url" in e for e in actions[1]["elements"])
    # status context is first, footer context is last.
    assert blocks[0]["elements"][0]["text"] == "Working"
    assert blocks[-1]["elements"][0]["text"] == "foot"


def test_backward_compatible_without_status_or_links() -> None:
    # Defaults (status None, links empty) add ZERO blocks: identical to today.
    reply = Reply(header="H", text="body", footer="f")
    blocks = to_blocks(reply)
    assert _types(blocks) == ["header", "section", "context"]
    # No status context leading, and no links actions block anywhere.
    assert not any(b["type"] == "actions" for b in blocks)
    assert blocks[0]["type"] == "header"  # header first, not a status context
