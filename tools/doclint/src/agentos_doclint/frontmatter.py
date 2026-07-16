"""Front-matter parse and validation.

Front-matter is the single declared source of truth for a seam's index row and
its generated header blockquote. An unvalidated contract is prose, so every
field is checked here.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

_REQUIRED_FIELDS = ("seam", "kind", "impls", "grade", "epics", "order")
_VALID_KINDS = ("CLEAN", "SOFT", "NONE")


@dataclass(frozen=True)
class SeamMeta:
    seam: str
    kind: str
    impls: str
    grade: str
    epics: list[str]
    order: int
    epic_note: str | None
    # The swap-readiness row this seam's grade answers to. Optional at parse
    # time because the eleven ungraded seams have no row; a *graded* seam
    # without one is a lint finding, not a parse error (#541, AC A).
    vision_row: str | None = None


@dataclass(frozen=True)
class FieldError:
    field: str
    reason: str


def split_front_matter(text: str) -> str | None:
    """Return the raw YAML front-matter block, or None if absent.

    The block is delimited by a leading ``---`` line and the next ``---`` line.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return None


def parse_and_validate(text: str) -> tuple[SeamMeta | None, list[FieldError]]:
    """Parse and validate a doc's front-matter.

    Returns ``(meta, [])`` when valid, ``(None, errors)`` otherwise. A missing
    block is reported as a missing ``front-matter`` field.
    """
    block = split_front_matter(text)
    if block is None:
        return None, [FieldError("front-matter", "missing front-matter block")]

    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        return None, [FieldError("front-matter", f"invalid YAML: {exc}")]
    if not isinstance(loaded, dict):
        return None, [FieldError("front-matter", "front-matter is not a mapping")]

    errors: list[FieldError] = []
    for field in _REQUIRED_FIELDS:
        if field not in loaded or loaded[field] is None:
            errors.append(FieldError(field, "front-matter field is required"))

    kind_ok = True
    if "kind" in loaded and loaded["kind"] is not None:
        kind_value = str(loaded["kind"])
        base_kind = kind_value.split(",", 1)[0].strip()
        if base_kind not in _VALID_KINDS:
            kind_ok = False
            errors.append(
                FieldError(
                    kind_value,
                    "invalid kind; expected CLEAN, SOFT, or NONE",
                )
            )

    order_value = 0
    if "order" in loaded and loaded["order"] is not None:
        try:
            order_value = int(loaded["order"])
        except (TypeError, ValueError):
            errors.append(FieldError("order", "front-matter field must be an integer"))

    epics: list[str] = []
    if "epics" in loaded and loaded["epics"] is not None:
        raw_epics = loaded["epics"]
        if not isinstance(raw_epics, list):
            errors.append(FieldError("epics", "front-matter field must be a list"))
        else:
            epics = [str(item) for item in raw_epics]

    if errors or not kind_ok:
        return None, errors

    epic_note_raw = loaded.get("epic_note")
    vision_row_raw = loaded.get("vision_row")
    meta = SeamMeta(
        seam=str(loaded["seam"]),
        kind=str(loaded["kind"]),
        impls=str(loaded["impls"]),
        grade=str(loaded["grade"]),
        epics=epics,
        order=order_value,
        epic_note=None if epic_note_raw is None else str(epic_note_raw),
        vision_row=None if vision_row_raw is None else str(vision_row_raw),
    )
    return meta, []
