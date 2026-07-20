// Fixture Rust source for cli/tests/api_field_parity.rs (issue #691).
//
// Parsed as TEXT by `syn`; this file is never compiled as a crate, so it needs
// no real dependencies and unused items are fine. Each `Deserialize` struct is
// engineered to trigger exactly one field-parity `Violation` (or none, for the
// serde-rename positive) against drifted-openapi.json + drifted-mirrors.json.
// The case map lives in api_field_parity.rs; keep the two in sync.

use serde::Deserialize;

// case 1: `dropped` is in the schema, not on the struct, not allowlisted -> MissingField.
#[derive(Debug, Clone, Deserialize)]
pub struct MissingFieldMirror {
    pub kept: String,
}

// case 2: the manifest declares `beta` omitted, but the struct carries it -> StaleOmission.
#[derive(Debug, Clone, Deserialize)]
pub struct StaleCarriesMirror {
    pub alpha: String,
    pub beta: String,
}

// case 3: the manifest declares `delta` omitted, but the schema no longer has it -> StaleOmission.
#[derive(Debug, Clone, Deserialize)]
pub struct StaleShrunkMirror {
    pub gamma: String,
}

// case 4: a Deserialize struct absent from both `mirrors` and `non_mirrors` -> UndeclaredStruct.
#[derive(Debug, Clone, Deserialize)]
pub struct UndeclaredMirror {
    pub anything: String,
}

// case 5: `phantom` is a wire field with no schema property behind it -> UnknownField.
#[derive(Debug, Clone, Deserialize)]
pub struct UnknownFieldMirror {
    pub known: String,
    pub phantom: String,
}

// case 6: the manifest points this at a schema absent from components.schemas -> SchemaNotFound.
#[derive(Debug, Clone, Deserialize)]
pub struct MissingSchemaMirror {
    pub y: String,
}

// case 7: the mirrored schema is object-level `allOf`-composed -> UnsupportedShape.
#[derive(Debug, Clone, Deserialize)]
pub struct AllOfMirror {
    pub z: String,
}

// case 8: a `#[serde(flatten)]` field makes the wire mapping non-local -> UnsupportedShape.
#[derive(Debug, Clone, Deserialize)]
pub struct FlattenMirror {
    pub base: String,
    #[serde(flatten)]
    pub extra: std::collections::HashMap<String, String>,
}

// case 9: the omission entry for `q` has a blank `why` -> StaleOmission.
#[derive(Debug, Clone, Deserialize)]
pub struct BlankWhyMirror {
    pub p: String,
}

// case 10 (positive): `foo_bar` renamed to the wire name `fooBar` covers the
// schema's `fooBar` property -> no violation. Proves the gate reads the WIRE name.
#[derive(Debug, Clone, Deserialize)]
pub struct RenamePositive {
    #[serde(rename = "fooBar")]
    pub foo_bar: String,
}

// case 10 (negative): same ident, no rename; the wire name stays `foo_bar`, so
// the schema's `fooBar` is uncovered -> MissingField (`fooBar`).
#[derive(Debug, Clone, Deserialize)]
pub struct RenameNegative {
    pub foo_bar: String,
}

// case 13: `discarded` is `#[serde(skip_deserializing)]`, so it is dropped from the
// wire (decoder fills it from Default) and must NOT cover the schema's `discarded`
// property -> MissingField. `kept` covers the schema's `kept` (so no UnknownField).
#[derive(Debug, Clone, Deserialize)]
pub struct SkipDeserMirror {
    pub kept: String,
    #[serde(skip_deserializing)]
    pub discarded: String,
}

// case 15: `MalformedEntryMirror` exists in the source but its `mirrors` entry omits
// the required `schema` key -> MalformedManifestEntry (would otherwise silently skip
// field comparison for this struct).
#[derive(Debug, Clone, Deserialize)]
pub struct MalformedEntryMirror {
    pub w: String,
}

// A non-Deserialize host type; carries the fn-body-local struct for case 11.
pub struct FixtureClient;

impl FixtureClient {
    // case 11: a Deserialize struct declared INSIDE an impl-method body, using the
    // qualified `serde::Deserialize` spelling (matching cli/src/api.rs:752), absent
    // from the manifest -> UndeclaredStruct. Proves the walk recurses into fn bodies.
    pub fn nested(&self) {
        #[derive(serde::Deserialize)]
        struct NestedUndeclared {
            files: Vec<String>,
        }
    }

    // case 14: two `Deserialize` structs share the bare name `DupNameMirror` (legal
    // Rust — each is fn-body-local). The manifest keys by name, so only the first is
    // ever field-checked; the walk sees both -> DuplicateStruct. Declared in
    // `non_mirrors` so DuplicateStruct is the ONLY violation this pair triggers.
    pub fn dup_a(&self) {
        #[derive(serde::Deserialize)]
        struct DupNameMirror {
            first: String,
        }
    }

    pub fn dup_b(&self) {
        #[derive(serde::Deserialize)]
        struct DupNameMirror {
            second: String,
        }
    }
}
