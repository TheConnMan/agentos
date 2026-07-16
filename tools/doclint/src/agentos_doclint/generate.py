"""The seam-table, ADR-index and per-doc header generator, plus marker handling.

The generator owns three generated regions, each delimited by HTML comment
markers so hand prose and generated content coexist in one file:

- the seam table in ``docs/interfaces.md``,
- the ADR index in ``docs/adr/README.md``,
- the header blockquote at the top of each ``INTERFACE.md``.

Seam rows are ordered by the front-matter ``order`` field; a tie is a hard
error, never a silent tiebreak, so output is byte-stable across runs and
machines. ADR rows are ordered by the number prefix in the filename, which the
uniqueness check guarantees is unique -- so that ordering is total for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import SeamMeta, parse_and_validate

INDEX_REL = "docs/interfaces.md"
SEAM_GLOB = "docs/interfaces/*/INTERFACE.md"

ADR_INDEX_REL = "docs/adr/README.md"
ADR_GLOB = "docs/adr/[0-9]*.md"

_TABLE_HEADER = "| Seam | Kind | Impls | Grade | Epic(s) | INTERFACE.md |"
_TABLE_SEPARATOR = "|---|---|---|---|---|---|"

_ADR_TABLE_HEADER = "| # | Decision | Status |"
_ADR_TABLE_SEPARATOR = "|---|---|---|"

# ``0042-llm-as-a-verifier-....md`` -> ("0042", "llm-as-a-verifier-...").
_ADR_FILENAME_RE = re.compile(r"^(\d+)-(.+)\.md$")
# The ``# 42. Adopt LLM-as-a-Verifier: ...`` heading; the number is dropped in
# favour of the filename's, which is the gated authority.
_ADR_HEADING_RE = re.compile(r"^#\s+(?:\d+\.\s*)?(.+?)\s*$", re.MULTILINE)
_ADR_STATUS_RE = re.compile(r"^Status:\s*(.+?)\s*$", re.MULTILINE)


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


def iter_adr_docs(repo_root: Path) -> list[Path]:
    """Every numbered ADR, found by glob (never a hardcoded list).

    ``README.md`` -- the index itself -- is excluded by the leading-digit glob,
    so the index is not a row in itself.
    """
    return sorted(repo_root.glob(ADR_GLOB))


def adr_number(doc: Path) -> str | None:
    """The zero-padded number prefix of an ADR filename, or None if unnumbered."""
    match = _ADR_FILENAME_RE.match(doc.name)
    return match.group(1) if match else None


def render_adr_row(doc: Path, text: str) -> str:
    """One index row: number from the filename, title and status from the file.

    The number comes from the **filename**, not the ``# 42.`` heading, because
    the filename is what the uniqueness check gates and what every inbound
    ``adr/0042-...`` link resolves against. The status is read verbatim -- ADR
    0011's ``Proposed (acceptance gated on the steer spike below)`` is a real
    status and the index must not flatten it to ``Proposed``.
    """
    number = adr_number(doc)
    heading = _ADR_HEADING_RE.search(text)
    status = _ADR_STATUS_RE.search(text)
    title = heading.group(1) if heading else doc.stem
    link = f"[{title}]({doc.name})"
    return f"| {number} | {link} | {status.group(1) if status else '(none)'} |"


def render_adr_table(repo_root: Path) -> str:
    """The ``docs/adr/README.md`` index block, ordered by number.

    Byte-stable across runs and machines; globs the ADR dir.
    """
    lines = [_ADR_TABLE_HEADER, _ADR_TABLE_SEPARATOR]
    for doc in iter_adr_docs(repo_root):
        lines.append(render_adr_row(doc, doc.read_text(encoding="utf-8")))
    return "\n".join(lines)


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
