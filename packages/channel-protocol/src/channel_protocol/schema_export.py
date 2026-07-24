"""Export the committed Curie channel message JSON Schema."""

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from .models import (
    Action,
    ChannelCapabilities,
    ChoiceIntent,
    ConfirmIntent,
    MessageField,
    MessageLink,
    OutboundMessage,
)

SCHEMA_ID = "https://curie.tech/curie/channel-protocol.schema.json"
_MODELS = (
    ChannelCapabilities,
    OutboundMessage,
    ChoiceIntent,
    ConfirmIntent,
    Action,
    MessageField,
    MessageLink,
)


def schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schema" / "channel-protocol.schema.json"


def build_schema() -> dict[str, Any]:
    _, top = models_json_schema(
        [(model, "validation") for model in _MODELS],
        ref_template="#/$defs/{model}",
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Curie Channel Protocol v1.0",
        "messageVersion": "1.0",
        **top,
    }


def render_schema() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


def write_schema() -> Path:
    path = schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(f"wrote {write_schema()}")
