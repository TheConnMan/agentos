"""The interface-catalog citation linter and index/header generator (#452).

Two phases run over the linted root (``docs/`` excluding ``docs/adr/``):

1. Generate: regenerate the seam table in ``docs/interfaces.md`` and each seam
   doc's header blockquote from front-matter, comparing to what is on disk.
2. Lint: walk every citation, assert no line coordinates, every path exists,
   every Python symbol resolves.

``main`` is a pure check by default (drift is a finding, nothing is written);
``main --write`` rewrites the generated regions so ``scripts/check-docs.sh`` can
diff them, mirroring ``scripts/check-contracts.sh``.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from .citation import (
    SOURCE_EXTENSIONS,
    Classification,
    CodeSpan,
    classify,
    find_code_spans,
    is_line_suppressed,
    path_exists,
    scan_raw_line_ban,
)
from .finding import Finding
from .frontmatter import SeamMeta, parse_and_validate
from .generate import (
    INDEX_REL,
    OrderTieError,
    extract_region,
    iter_seam_docs,
    render_header_block,
    render_index_table,
    render_table,
    replace_region,
    seam_link,
)
from .symbols import SymbolCache, SymbolSyntaxError, resolve_symbol

__all__ = [
    "SOURCE_EXTENSIONS",
    "Finding",
    "lint",
    "main",
    "render_index_table",
]

_ADR_REL = "docs/adr"


def _git_top_level() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def lint(repo_root: Path) -> list[Finding]:
    """Run the generate + lint phases in memory and return the findings."""
    findings, _ = _lint_and_count(repo_root)
    return findings


def _lint_and_count(repo_root: Path) -> tuple[list[Finding], int]:
    """Return the findings plus the count of lines silenced by the escape hatch."""
    cache = SymbolCache()
    findings: list[Finding] = []
    findings.extend(_check_generation(repo_root))
    doc_findings, ignored = _check_docs(repo_root, cache)
    findings.extend(doc_findings)
    return findings, ignored


def _collect_seam_rows(
    repo_root: Path,
) -> tuple[list[tuple[Path, str, str, SeamMeta]], list[Finding]]:
    """Parse every seam doc's front-matter once.

    Returns the docs that parsed cleanly, each as ``(doc, rel, text, meta)``,
    plus findings for any doc whose front-matter failed to parse. Shared by
    the check phase and the write phase so their seam-row loops cannot drift
    out of sync with each other.
    """
    findings: list[Finding] = []
    seam_docs: list[tuple[Path, str, str, SeamMeta]] = []

    for doc in iter_seam_docs(repo_root):
        rel = doc.relative_to(repo_root).as_posix()
        text = doc.read_text(encoding="utf-8")
        meta, errors = parse_and_validate(text)
        if meta is None:
            findings.extend(Finding(rel, err.field, err.reason) for err in errors)
            continue
        seam_docs.append((doc, rel, text, meta))

    return seam_docs, findings


def _render_table_or_finding(
    rows: list[tuple[str, SeamMeta]],
) -> tuple[str | None, Finding | None]:
    """Render the seam table, converting an ``OrderTieError`` into a Finding.

    Returns ``(table, None)`` on success or ``(None, finding)`` on a tie, so
    the check phase and the write phase report a tie identically.
    """
    try:
        return render_table(rows), None
    except OrderTieError as exc:
        return None, Finding(INDEX_REL, "seam-table", f"order tie: {exc}")


def _check_generation(repo_root: Path) -> list[Finding]:
    seam_docs, findings = _collect_seam_rows(repo_root)
    rows: list[tuple[str, SeamMeta]] = []

    for doc, rel, text, meta in seam_docs:
        rows.append((seam_link(doc, repo_root), meta))
        findings.extend(_check_header_region(rel, text, meta))

    findings.extend(_check_index_region(repo_root, rows))
    return findings


def _check_header_region(rel: str, text: str, meta: SeamMeta) -> list[Finding]:
    region = extract_region(text, "header")
    if region.missing:
        return [
            Finding(rel, "header", "generated header marker block is missing or unpaired")
        ]
    if region.content.strip() != render_header_block(meta).strip():
        return [Finding(rel, "header", "generated header block is out of date; regenerate")]
    return []


def _check_index_region(repo_root: Path, rows: list[tuple[str, SeamMeta]]) -> list[Finding]:
    index_path = repo_root / INDEX_REL
    if not index_path.is_file():
        return []
    text = index_path.read_text(encoding="utf-8")
    region = extract_region(text, "seam-table")
    if region.missing:
        return [Finding(INDEX_REL, "seam-table", "generated marker block is missing or unpaired")]
    expected, tie_finding = _render_table_or_finding(rows)
    if tie_finding is not None:
        return [tie_finding]
    if expected is not None and region.content.strip() != expected.strip():
        return [Finding(INDEX_REL, "seam-table", "index seam-table is out of date; regenerate")]
    return []


def _check_docs(repo_root: Path, cache: SymbolCache) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    ignored = 0
    docs_root = repo_root / "docs"
    if not docs_root.is_dir():
        return findings, ignored
    adr_root = repo_root / _ADR_REL

    for md in sorted(docs_root.rglob("*.md")):
        if _is_under(md, adr_root):
            continue
        rel = md.relative_to(repo_root).as_posix()
        text = md.read_text(encoding="utf-8")
        doc_findings, doc_ignored = _lint_doc(repo_root, rel, text, cache)
        findings.extend(doc_findings)
        ignored += doc_ignored

    return findings, ignored


def _lint_doc(
    repo_root: Path, rel: str, text: str, cache: SymbolCache
) -> tuple[list[Finding], int]:
    """Lint one doc, then apply the per-line escape hatch uniformly.

    Every finding type (line-ban, path, symbol, shorthand) is produced first,
    then a finding whose line is silenced by ``<!-- doclint:ignore-line -->``
    (on the preceding line, or inline at the end of that same line) is dropped.
    The count is of distinct suppressed physical lines, so the summary reflects
    genuinely-silenced findings, not bare comments.
    """
    lines = text.splitlines()
    raw: list[Finding] = []

    for hit in scan_raw_line_ban(text):
        raw.append(
            Finding(
                rel,
                hit.coordinate,
                "line-number coordinate citation is banned; cite a symbol instead",
                line=hit.line,
            )
        )

    for span in find_code_spans(text):
        raw.extend(_check_span(repo_root, rel, span, cache))

    kept: list[Finding] = []
    suppressed_lines: set[int] = set()
    for finding in raw:
        if finding.line is not None and is_line_suppressed(lines, finding.line):
            suppressed_lines.add(finding.line)
        else:
            kept.append(finding)

    return kept, len(suppressed_lines)


def _check_span(repo_root: Path, rel: str, span: CodeSpan, cache: SymbolCache) -> list[Finding]:
    classification = classify(span.content)
    if classification.kind == "not_a_citation":
        return []
    if classification.kind == "shorthand_error":
        return [
            Finding(
                rel,
                span.content,
                "shorthand citation has a symbol but no path; write the full "
                "repo-relative path (citations resolve from the repo root, "
                "never relative to the doc)",
                line=span.line,
            )
        ]
    return _check_citation(repo_root, rel, span, classification, cache)


def _check_citation(
    repo_root: Path,
    rel: str,
    span: CodeSpan,
    classification: Classification,
    cache: SymbolCache,
) -> list[Finding]:
    if not path_exists(repo_root, classification.path):
        return [Finding(rel, span.content, "cited path does not exist", line=span.line)]
    if not classification.symbol:
        return []

    extension = classification.path.rsplit(".", 1)[-1]
    if extension != "py":
        return [
            Finding(
                rel,
                span.content,
                f"symbol resolution is not supported for .{extension}; cite the file only",
                line=span.line,
            )
        ]

    try:
        resolved = resolve_symbol(repo_root / classification.path, classification.symbol, cache)
    except SymbolSyntaxError:
        return [Finding(rel, span.content, "cited file has a syntax error", line=span.line)]
    if not resolved:
        return [
            Finding(
                rel,
                span.content,
                f"symbol '{classification.symbol}' does not resolve",
                line=span.line,
            )
        ]
    return []


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _write_generated(repo_root: Path) -> list[Finding]:
    """Rewrite the generated regions in place; return blocking findings."""
    seam_docs, findings = _collect_seam_rows(repo_root)
    rows: list[tuple[str, SeamMeta]] = []

    for doc, rel, text, meta in seam_docs:
        rows.append((seam_link(doc, repo_root), meta))
        region = extract_region(text, "header")
        if region.missing:
            findings.append(
                Finding(rel, "header", "generated header marker block is missing or unpaired")
            )
            continue
        rewritten = replace_region(text, "header", render_header_block(meta))
        if rewritten != text:
            doc.write_text(rewritten, encoding="utf-8")

    index_path = repo_root / INDEX_REL
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8")
        region = extract_region(text, "seam-table")
        if region.missing:
            findings.append(
                Finding(INDEX_REL, "seam-table", "generated marker block is missing or unpaired")
            )
        else:
            table, tie_finding = _render_table_or_finding(rows)
            if tie_finding is not None:
                findings.append(tie_finding)
            elif table is not None:
                rewritten = replace_region(text, "seam-table", table)
                if rewritten != text:
                    index_path.write_text(rewritten, encoding="utf-8")

    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentos_doclint")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to lint; defaults to the git top-level.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite the generated regions in place instead of checking drift.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else _git_top_level()

    if args.write:
        findings = _write_generated(repo_root)
        ignored = 0
    else:
        findings, ignored = _lint_and_count(repo_root)

    for finding in findings:
        print(finding.render())

    if ignored:
        print(f"doclint: {ignored} line(s) suppressed by the ignore-line escape hatch")

    return 0 if not findings else 1
