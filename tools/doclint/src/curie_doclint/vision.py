"""The swap-readiness table in ``docs/architecture-vision.md`` (#541, AC A).

Every graded seam doc names this table as the authority for its ``grade:``,
but nothing checked the claim, so ``blob-storage`` sat at ``A-`` against the
table's ``B+`` indefinitely. Parsing the table here makes it machine-checked
authority instead of prose a doc happens to cite.

A grade cell reads ``B+: config-only within S3-compatible stores; ...`` -- a
grade token, a colon, then the rationale. Only the token is the grade.
"""

from __future__ import annotations

from pathlib import Path

VISION_REL = "docs/architecture-vision.md"

_SECTION_HEADING = "## Swap readiness"
_JOB_COLUMN = 1
_GRADE_COLUMN = 4


def _grade_token(cell: str) -> str:
    """The grade token: everything before the rationale's colon."""
    return cell.split(":", 1)[0].strip()


def read_swap_readiness(repo_root: Path) -> dict[str, str]:
    """Map each swap-readiness row's job name to its grade token.

    Returns an empty mapping when the doc or the section is absent. That is not
    a silent pass: a graded seam whose declared row is missing from the mapping
    is a finding, so an absent table fails every graded seam loudly.
    """
    path = repo_root / VISION_REL
    if not path.is_file():
        return {}

    lines = path.read_text(encoding="utf-8").splitlines()
    grades: dict[str, str] = {}
    in_section = False

    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip() == _SECTION_HEADING
            continue
        if not in_section or not line.startswith("|"):
            continue
        columns = line.split("|")
        if len(columns) <= _GRADE_COLUMN:
            continue
        job = columns[_JOB_COLUMN].strip()
        if not job or job == "Job" or set(job) <= {"-", ":", " "}:
            continue
        grades[job] = _grade_token(columns[_GRADE_COLUMN])

    return grades
