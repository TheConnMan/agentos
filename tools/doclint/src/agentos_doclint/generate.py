"""The seam-table and per-doc header generator, plus marker handling.

The generator owns two generated regions, each delimited by HTML comment
markers so hand prose and generated content coexist in one file:

- the seam table in ``docs/interfaces.md``,
- the header blockquote at the top of each ``INTERFACE.md``.

Rows are ordered by the front-matter ``order`` field; a tie is a hard error,
never a silent tiebreak, so output is byte-stable across runs and machines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .frontmatter import SeamMeta, parse_and_validate

INDEX_REL = "docs/interfaces.md"
SEAM_GLOB = "docs/interfaces/*/INTERFACE.md"

_TABLE_HEADER = "| Seam | Kind | Impls | Grade | Epic(s) | INTERFACE.md |"
_TABLE_SEPARATOR = "|---|---|---|---|---|---|"


class OrderTieError(Exception):
    """Raised when two seams declare the same ``order`` value."""


def iter_seam_docs(repo_root: Path) -> list[Path]:
    """Every seam INTERFACE.md, found by glob (never a hardcoded list)."""
    return sorted(repo_root.glob(SEAM_GLOB))


def seam_link(doc: Path, repo_root: Path) -> str:
    """The index link path for a seam doc, relative to ``docs/``."""
    return doc.relative_to(repo_root / "docs").as_posix()


def _epics_cell(meta: SeamMeta) -> str:
    joined = ", ".join(meta.epics)
    if meta.epic_note:
        return f"({meta.epic_note} {joined})"
    return joined


def render_row(meta: SeamMeta, link: str) -> str:
    return (
        f"| {meta.seam} | {meta.kind} | {meta.impls} | {meta.grade} "
        f"| {_epics_cell(meta)} | [{meta.seam}]({link}) |"
    )


def render_table(rows: list[tuple[str, SeamMeta]]) -> str:
    """Render the seam table from ``(link, meta)`` pairs, ordered by ``order``.

    A repeated ``order`` value is a hard error.
    """
    seen: dict[int, str] = {}
    for _, meta in rows:
        if meta.order in seen:
            raise OrderTieError(
                f"order {meta.order} is declared by both "
                f"{seen[meta.order]} and {meta.seam}"
            )
        seen[meta.order] = meta.seam

    ordered = sorted(rows, key=lambda pair: pair[1].order)
    lines = [_TABLE_HEADER, _TABLE_SEPARATOR]
    lines.extend(render_row(meta, link) for link, meta in ordered)
    return "\n".join(lines)


def render_index_table(repo_root: Path) -> str:
    """Public generator surface: the ``docs/interfaces.md`` seam-table block.

    Byte-stable across runs and machines; globs the interfaces dir.
    """
    rows: list[tuple[str, SeamMeta]] = []
    for doc in iter_seam_docs(repo_root):
        meta, errors = parse_and_validate(doc.read_text(encoding="utf-8"))
        if meta is None:
            raise ValueError(f"{doc}: invalid front-matter: {errors}")
        rows.append((seam_link(doc, repo_root), meta))
    return render_table(rows)


def render_header_block(meta: SeamMeta) -> str:
    """The generated header blockquote for a seam doc."""
    return (
        f"> **Kind:** {meta.kind} &nbsp;·&nbsp; "
        f"**Implementations today:** {meta.impls} &nbsp;·&nbsp; "
        f"**Swap-readiness grade:** {meta.grade}"
    )


@dataclass(frozen=True)
class RegionResult:
    missing: bool
    content: str = ""


def _begin_prefix(name: str) -> str:
    return f"<!-- BEGIN GENERATED: {name}"


def _end_marker(name: str) -> str:
    return f"<!-- END GENERATED: {name} -->"


def extract_region(text: str, name: str) -> RegionResult:
    """Return the content between the named markers, or missing/unpaired."""
    begin = text.find(_begin_prefix(name))
    if begin == -1:
        return RegionResult(missing=True)
    begin_close = text.find("-->", begin)
    if begin_close == -1:
        return RegionResult(missing=True)
    content_start = begin_close + len("-->")
    end = text.find(_end_marker(name), content_start)
    if end == -1:
        return RegionResult(missing=True)
    return RegionResult(missing=False, content=text[content_start:end])


def replace_region(text: str, name: str, new_content: str) -> str:
    """Rewrite the content between the named markers, preserving the markers."""
    begin = text.find(_begin_prefix(name))
    begin_close = text.find("-->", begin) + len("-->")
    end = text.find(_end_marker(name), begin_close)
    return f"{text[:begin_close]}\n{new_content}\n{text[end:]}"
