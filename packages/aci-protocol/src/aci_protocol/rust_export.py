"""Generate Rust serde types for the ACI protocol from the Pydantic models.

The Rust CLI (task I1) speaks the ACI over HTTP and NDJSON, so it needs types
that match the frozen contract exactly. Rather than depend on a JSON-Schema-to-
Rust toolchain, this module introspects the same Pydantic models the schema is
built from and emits idiomatic serde structs and internally tagged enums. Output
is deterministic, so the compat gate regenerates and diffs it; a model change
that is not reflected in the committed Rust fails the build.

Run as ``python -m aci_protocol.rust_export`` to rewrite the committed crate.
"""

import types as _types
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from .events import (
    ErrorEvent,
    Event,
    Final,
    Interrupt,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from .session import Budget, OtelConfig, SessionConfig
from .version import PROTOCOL_VERSION

_NONE = type(None)
_SCALARS: dict[type, str] = {str: "String", int: "i64", float: "f64", bool: "bool"}

# Multi-valued string literals map to a dedicated Rust enum. Only Event.type
# exists today; an unrecognized literal raises so the generator stays honest.
_EVENT_TYPE_ARGS = get_args(Event.model_fields["type"].annotation)

# Tagged enums cannot use deny_unknown_fields (serde does not support it with
# internally tagged enums), so the wire-strictness of extra="forbid" is enforced
# on the plain structs, which do carry deny_unknown_fields.
_ENUM_DERIVES = "#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]"
_STRUCT_DERIVES = "#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]"

# Rust keywords that a field name may collide with, requiring a raw identifier.
_RUST_KEYWORDS = {
    "type",
    "match",
    "move",
    "ref",
    "self",
    "impl",
    "fn",
    "use",
    "mod",
    "as",
    "let",
    "loop",
    "enum",
    "struct",
    "trait",
    "crate",
    "super",
    "in",
    "box",
    "dyn",
    "async",
    "await",
}


def _rust_field(name: str) -> str:
    return f"r#{name}" if name in _RUST_KEYWORDS else name


def _is_wire_const(annotation: Any) -> bool:
    """True for a single-valued string literal (the version const).

    Such fields have a Python default but are mandatory on the wire, so Rust must
    require them rather than defaulting them, matching the JSON Schema.
    """

    if get_origin(annotation) is Literal:
        args = get_args(annotation)
        return len(args) == 1 and isinstance(args[0], str)
    return False


def crate_dir() -> Path:
    """The committed generated Rust crate directory inside this package."""

    return Path(__file__).resolve().parents[2] / "generated" / "rust"


def _pascal(token: str) -> str:
    return "".join(part.capitalize() for part in token.replace("-", "_").split("_"))


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        args = [a for a in get_args(annotation) if a is not _NONE]
        if len(args) != 1:
            raise TypeError(f"only Optional[T] unions are supported, got {annotation!r}")
        return args[0], True
    return annotation, False


def _rust_type(annotation: Any) -> str:
    inner, optional = _split_optional(annotation)
    rust = _rust_bare_type(inner)
    return f"Option<{rust}>" if optional else rust


def _rust_bare_type(annotation: Any) -> str:
    if annotation in _SCALARS:
        return _SCALARS[annotation]
    origin = get_origin(annotation)
    if origin is list:
        return f"Vec<{_rust_type(get_args(annotation)[0])}>"
    if origin is Literal:
        args = get_args(annotation)
        if args == _EVENT_TYPE_ARGS:
            return "EventType"
        if len(args) == 1 and isinstance(args[0], str):
            # A single-valued string literal (the version const) is a plain
            # String on the Rust side; the NDJSON decoder enforces the value.
            return "String"
        raise TypeError(f"unexpected literal field {annotation!r}")
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation.__name__
    raise TypeError(f"no Rust mapping for {annotation!r}")


def _string_enum(name: str, values: tuple[str, ...], default: str | None = None) -> str:
    # Derive Default (with a #[default] variant) only when a defaulted field
    # references this enum, so serde(default) on that field compiles.
    derives = _STRUCT_DERIVES if default is not None else _ENUM_DERIVES
    lines = [derives, f"pub enum {name} {{"]
    for value in values:
        lines.append(f'    #[serde(rename = "{value}")]')
        if value == default:
            lines.append("    #[default]")
        lines.append(f"    {_pascal(value)},")
    lines.append("}")
    return "\n".join(lines)


def _struct_fields(model: type[BaseModel], skip: set[str], public: bool) -> list[str]:
    out: list[str] = []
    prefix = "pub " if public else ""
    for field_name, field in model.model_fields.items():
        if field_name in skip:
            continue
        rust = _rust_type(field.annotation)
        if _is_wire_const(field.annotation):
            # A wire constant (version) is mandatory and value-checked on decode,
            # matching the NDJSON decoder's exact-match version policy.
            out.append('    #[serde(deserialize_with = "require_protocol_version")]')
        elif not field.is_required():
            # Any other field with a Pydantic default is omittable on the wire,
            # so Rust accepts it missing too.
            out.append("    #[serde(default)]")
        out.append(f"    {prefix}{_rust_field(field_name)}: {rust},")
    return out


def _struct(model: type[BaseModel]) -> str:
    lines = [_STRUCT_DERIVES, "#[serde(deny_unknown_fields)]", f"pub struct {model.__name__} {{"]
    lines.extend(_struct_fields(model, skip=set(), public=True))
    lines.append("}")
    return "\n".join(lines)


def _tagged_enum(name: str, tag: str, variants: tuple[type[BaseModel], ...]) -> str:
    lines = [
        _ENUM_DERIVES,
        f'#[serde(tag = "{tag}", deny_unknown_fields)]',
        f"pub enum {name} {{",
    ]
    for model in variants:
        tag_value = get_args(model.model_fields[tag].annotation)[0]
        lines.append(f'    #[serde(rename = "{tag_value}")]')
        lines.append(f"    {model.__name__} {{")
        for field_line in _struct_fields(model, skip={tag}, public=False):
            lines.append(f"    {field_line}")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


_VERSION_GUARD = """fn require_protocol_version<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = String::deserialize(deserializer)?;
    if value != PROTOCOL_VERSION {
        return Err(serde::de::Error::custom(format!(
            "unsupported protocol version {value:?}; this build speaks {PROTOCOL_VERSION:?}"
        )));
    }
    Ok(value)
}"""


_TESTS = """#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn final_event_roundtrips() {
        let event = OutboundEvent::Final {
            version: PROTOCOL_VERSION.to_string(),
            text: "hi".to_string(),
            status: SessionStatus::Done,
        };
        let encoded = serde_json::to_string(&event).unwrap();
        let decoded: OutboundEvent = serde_json::from_str(&encoded).unwrap();
        assert_eq!(event, decoded);
    }

    #[test]
    fn inbound_event_roundtrips() {
        let message = InboundMessage::Event {
            r#type: EventType::Message,
            text: "hello".to_string(),
            user: "U1".to_string(),
            ts: "1.0".to_string(),
        };
        let encoded = serde_json::to_string(&message).unwrap();
        let decoded: InboundMessage = serde_json::from_str(&encoded).unwrap();
        assert_eq!(message, decoded);
    }

    #[test]
    fn rejects_off_version_event() {
        let raw = r#"{"type":"final","version":"9.9.9","text":"x","status":"done"}"#;
        assert!(serde_json::from_str::<OutboundEvent>(raw).is_err());
    }

    #[test]
    fn rejects_unknown_fields() {
        let raw = r#"{"type":"final","version":"0.1.0","text":"x","status":"done","extra":1}"#;
        assert!(serde_json::from_str::<OutboundEvent>(raw).is_err());
    }
}
"""


def render_rust() -> str:
    """Render the full generated lib.rs as a deterministic string."""

    blocks = [
        "// GENERATED by aci_protocol.rust_export. Do not edit by hand.",
        "// Regenerate with: python -m aci_protocol.rust_export",
        "#![allow(dead_code)]",
        "use serde::{Deserialize, Serialize};",
        f'pub const PROTOCOL_VERSION: &str = "{PROTOCOL_VERSION}";',
        _VERSION_GUARD,
        _string_enum(
            "SessionStatus",
            tuple(m.value for m in SessionStatus),
            default=SessionStatus.DONE.value,
        ),
        _string_enum("EventType", _EVENT_TYPE_ARGS),
        _struct(Budget),
        _struct(OtelConfig),
        _struct(SessionConfig),
        _tagged_enum("InboundMessage", "kind", (Event, Interrupt)),
        _tagged_enum(
            "OutboundEvent",
            "type",
            (TextDelta, ToolNote, Final, ErrorEvent, SideEffectFlag),
        ),
        _TESTS.rstrip("\n"),
    ]
    return "\n\n".join(blocks) + "\n"


def write_rust() -> Path:
    """Write the generated lib.rs to the committed crate and return its path."""

    path = crate_dir() / "src" / "lib.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_rust(), encoding="utf-8")
    return path


if __name__ == "__main__":
    written = write_rust()
    print(f"wrote {written}")
