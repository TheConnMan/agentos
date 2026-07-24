"""Markdown -> Slack mrkdwn conversion (the chat.update render fix)."""

from __future__ import annotations

from curie_worker.mrkdwn import to_mrkdwn


def test_bold_double_asterisk_becomes_single() -> None:
    assert to_mrkdwn("**bold** here") == "*bold* here"


def test_link_becomes_angle_pipe() -> None:
    assert to_mrkdwn("see [the docs](https://x.com/a)") == "see <https://x.com/a|the docs>"


def test_h2_heading_becomes_bold_line() -> None:
    assert to_mrkdwn("## Results") == "*Results*"


def test_h1_and_h3_headings_become_bold_lines() -> None:
    assert to_mrkdwn("# Top\n### Deep") == "*Top*\n*Deep*"


def test_heading_with_trailing_hashes_is_stripped() -> None:
    assert to_mrkdwn("## Title ##") == "*Title*"


def test_dash_bullet_becomes_bullet_char() -> None:
    assert to_mrkdwn("- one\n- two") == "• one\n• two"


def test_asterisk_bullet_becomes_bullet_char() -> None:
    assert to_mrkdwn("* one\n* two") == "• one\n• two"


def test_nested_bullet_preserves_indentation() -> None:
    assert to_mrkdwn("- top\n  - nested") == "• top\n  • nested"


def test_bullet_inside_fenced_code_is_unchanged() -> None:
    text = "list:\n```\n- code bullet\n* also code\n```"
    expected = "list:\n```\n- code bullet\n* also code\n```"
    assert to_mrkdwn(text) == expected


def test_italic_line_is_not_a_bullet() -> None:
    # A leading `*` with no trailing space is italic, not a bullet: left alone.
    assert to_mrkdwn("*em* text") == "*em* text"


def test_numbered_list_is_left_alone() -> None:
    assert to_mrkdwn("1. first\n2. second") == "1. first\n2. second"


def test_mixed_doc_converts_bullets_bold_and_leaves_code() -> None:
    text = "## Plan\n- **do** this\n  - see [docs](http://z)\n```\n- literal\n```"
    expected = "*Plan*\n• *do* this\n  • see <http://z|docs>\n```\n- literal\n```"
    assert to_mrkdwn(text) == expected


def test_inline_code_is_preserved_verbatim() -> None:
    # ** and [](...) inside a code span must not be rewritten.
    assert to_mrkdwn("use `**not bold** [x](y)`") == "use `**not bold** [x](y)`"


def test_fenced_code_block_is_preserved_verbatim() -> None:
    text = "before\n```\n**still code** [x](y)\n## nope\n```\nafter **bold**"
    expected = "before\n```\n**still code** [x](y)\n## nope\n```\nafter *bold*"
    assert to_mrkdwn(text) == expected


def test_combined_bold_link_and_heading() -> None:
    text = "## Summary\n**Done**: see [here](http://z)"
    assert to_mrkdwn(text) == "*Summary*\n*Done*: see <http://z|here>"


def test_empty_string_is_unchanged() -> None:
    assert to_mrkdwn("") == ""


def test_plain_text_is_unchanged() -> None:
    assert to_mrkdwn("just a normal sentence.") == "just a normal sentence."


def test_hash_without_space_is_not_a_heading() -> None:
    assert to_mrkdwn("#hashtag stays") == "#hashtag stays"
