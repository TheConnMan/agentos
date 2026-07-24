"""Emit the committed OpenAPI document.

The committed apps/api/openapi.json is a generated artifact; test_openapi_drift
fails the build if it drifts from the live app. Run as
``python -m curie_api.export_openapi`` to rewrite it after an intended change.
"""

import json
from pathlib import Path
from typing import Any

from .main import create_app


def openapi_path() -> Path:
    return Path(__file__).resolve().parents[2] / "openapi.json"


def build_openapi() -> dict[str, Any]:
    return create_app().openapi()


def render_openapi() -> str:
    return json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n"


def write_openapi() -> Path:
    path = openapi_path()
    path.write_text(render_openapi(), encoding="utf-8")
    return path


if __name__ == "__main__":
    written = write_openapi()
    print(f"wrote {written}")
