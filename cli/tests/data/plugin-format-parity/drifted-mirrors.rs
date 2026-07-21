// Fixture source for the plugin_format field-parity gate's rejection tests
// (issue #701). Deliberately drifted from the real ApprovalGate schema so the
// gate's assertions prove it rejects each drift class by execution, mirroring
// how cli/tests/data/field-parity/drifted-api.rs proves the same for #691.

use serde::Deserialize;

/// Case 1: covers only `gate`, so the required `route` schema property is
/// uncovered -> `MissingField`.
#[derive(Deserialize)]
struct MissingFieldGate {
    gate: Option<String>,
}

/// Case 2: a `Deserialize` struct present in the source but declared in
/// neither `mirrors` nor `non_mirrors` -> `UndeclaredStruct`.
#[derive(Deserialize)]
struct UndeclaredMirror {
    name: Option<String>,
}

/// Case 3: carries a wire field (`ghost_field`) the schema does not define
/// -> `UnknownField`. No allowlist path for this direction: a CLI field with
/// no schema field behind it is always a bug.
#[derive(Deserialize)]
struct PhantomFieldGate {
    gate: Option<String>,
    route: Option<String>,
    ghost_field: Option<String>,
}
