"""Export the eval-case models to a canonical JSON Schema document.

Committed and drift-checked exactly like the plugin-format schema. Run as
``python -m agentos_worker.eval.schema_export`` to rewrite the committed schema.
"""

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from .models import EvalCase, EvalSuite, Grader

_MODELS = (EvalSuite, EvalCase, Grader)

SCHEMA_ID = "https://curie.tech/agentos/eval-cases.schema.json"


def schema_path() -> Path:
    """The committed schema file location inside apps/worker."""

    return Path(__file__).resolve().parents[3] / "schema" / "eval-cases.schema.json"


def build_schema() -> dict[str, Any]:
    """Build the combined JSON Schema document for the eval-case models."""

    _, top = models_json_schema(
        [(model, "validation") for model in _MODELS],
        ref_template="#/$defs/{model}",
    )
    doc: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "AgentOS Eval Case Format",
    }
    doc.update(top)
    return doc


def render_schema() -> str:
    """Render the canonical schema string (sorted keys, trailing newline)."""

    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


def write_schema() -> Path:
    """Write the canonical schema to its committed location and return the path."""

    path = schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(), encoding="utf-8")
    return path


if __name__ == "__main__":
    written = write_schema()
    print(f"wrote {written}")
