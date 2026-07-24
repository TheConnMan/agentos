//! Inventory contract gate for the versioned CLI result schemas (issue #634).
//!
//! AC2's anti-drift mechanism: `cli/schema/index.json` maps every agent-facing
//! `--json` result family to a committed, versioned JSON Schema, and this gate
//! proves that inventory honest against the real tree -- every `impl CliOutput`
//! is declared with a schema, every declared schema file exists and compiles,
//! every entry's `version` agrees with the schema's `$id`, and no new
//! direct-`emit_json` result has slipped in unpinned. A new result family that
//! lands without a schema fails here. The rejection cases below drive the same
//! pure comparator over a drifted fixture to prove it rejects each class by
//! execution, mirroring `cli/tests/api_field_parity.rs`.

#[path = "support/schema_inventory.rs"]
mod schema_inventory;

use std::collections::BTreeMap;

use schema_inventory::{version_from_id, violations, SchemaState, Violation};
use serde_json::Value;

// ─── Loaders ─────────────────────────────────────────────────────────────────

fn repo_path(rel: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join(rel)
}

/// Every `cli/src/*.rs` file as `(repo-relative path, contents)`. The path form
/// matches the `raw_emit_sites` keys in `index.json`.
fn cli_sources() -> Vec<(String, String)> {
    let dir = repo_path("cli/src");
    let mut out = Vec::new();
    for entry in std::fs::read_dir(&dir).expect("read cli/src") {
        let entry = entry.expect("dir entry");
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("rs") {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            let text = std::fs::read_to_string(&path).expect("read source");
            out.push((format!("cli/src/{name}"), text));
        }
    }
    out.sort();
    out
}

fn borrowed(srcs: &[(String, String)]) -> Vec<(&str, &str)> {
    srcs.iter().map(|(p, s)| (p.as_str(), s.as_str())).collect()
}

fn index_json() -> Value {
    let raw = std::fs::read_to_string(repo_path("cli/schema/index.json")).expect("read index.json");
    serde_json::from_str(&raw).expect("index.json is valid JSON")
}

/// The committed schema files, each described by whether it compiles to a
/// validator and the version its `$id` declares.
fn on_disk_schemas() -> BTreeMap<String, SchemaState> {
    let dir = repo_path("cli/schema");
    let mut out = BTreeMap::new();
    for entry in std::fs::read_dir(&dir).expect("read cli/schema") {
        let entry = entry.expect("dir entry");
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.ends_with(".schema.json") {
            continue;
        }
        let raw = std::fs::read_to_string(entry.path()).expect("read schema");
        let value: Value = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("committed schema {name} is not valid JSON: {e}"));
        let compiles = jsonschema::validator_for(&value).is_ok();
        let id_version = value
            .get("$id")
            .and_then(Value::as_str)
            .and_then(version_from_id);
        out.insert(
            name,
            SchemaState {
                compiles,
                id_version,
            },
        );
    }
    out
}

// ─── Real-tree assertions (AC1, AC2, AC4 identity) ───────────────────────────

#[test]
fn the_real_inventory_has_no_drift() {
    let srcs = cli_sources();
    let vs = violations(&borrowed(&srcs), &index_json(), &on_disk_schemas());
    assert!(
        vs.is_empty(),
        "cli/schema/index.json has drifted from cli/src. Each entry is fixed by either \
         adding an index.json entry + committed schema for a new result family, or \
         correcting the named schema/version:\n{vs:#?}"
    );
}

#[test]
fn every_committed_schema_compiles_and_declares_a_version() {
    // Independent of the comparator: a directly-stated invariant so a schema
    // that neither compiles nor carries a `/vN` `$id` cannot hide behind a
    // matching (also-broken) index entry.
    for (name, state) in on_disk_schemas() {
        assert!(state.compiles, "schema {name} must compile to a validator");
        assert!(
            state.id_version.is_some(),
            "schema {name} must carry a versioned $id like .../{{name}}/v1.json"
        );
    }
}

#[test]
fn version_from_id_parses_the_v_segment() {
    assert_eq!(
        version_from_id("https://schemas.curie.dev/cli/kill/v1.json"),
        Some(1)
    );
    assert_eq!(version_from_id(".../foo/v12.json"), Some(12));
    assert_eq!(version_from_id(".../foo/bar.json"), None);
    assert_eq!(version_from_id(".../foo/vX.json"), None);
}

// ─── Guard-rejects-a-violating-input demonstrations (AC2 teeth) ──────────────

fn fixture_src() -> String {
    std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/data/schema-inventory/drifted-src.rs"),
    )
    .expect("read drifted-src.rs")
}

fn fixture_index() -> Value {
    let raw = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/data/schema-inventory/drifted-index.json"),
    )
    .expect("read drifted-index.json");
    serde_json::from_str(&raw).expect("drifted-index.json is valid JSON")
}

fn fixture_schemas() -> BTreeMap<String, SchemaState> {
    BTreeMap::from([
        (
            "good.schema.json".to_string(),
            SchemaState {
                compiles: true,
                id_version: Some(1),
            },
        ),
        (
            "invalid.schema.json".to_string(),
            SchemaState {
                compiles: false,
                id_version: None,
            },
        ),
        (
            "v2.schema.json".to_string(),
            SchemaState {
                compiles: true,
                id_version: Some(2),
            },
        ),
        // "absent.schema.json" is deliberately not here.
    ])
}

fn fixture_violations() -> Vec<Violation> {
    let src = fixture_src();
    let srcs = vec![("drifted.rs".to_string(), src)];
    violations(&borrowed(&srcs), &fixture_index(), &fixture_schemas())
}

#[test]
fn rejects_an_impl_cli_output_with_no_index_entry() {
    // THE anti-drift teeth: a new result family with no schema.
    let vs = fixture_violations();
    assert!(
        vs.iter().any(
            |v| matches!(v, Violation::UndeclaredResult { result } if result == "UndeclaredOne")
        ),
        "{vs:#?}"
    );
}

#[test]
fn rejects_an_index_entry_with_no_impl() {
    let vs = fixture_violations();
    assert!(
        vs.iter()
            .any(|v| matches!(v, Violation::ResultNotFound { result } if result == "GhostImpl")),
        "{vs:#?}"
    );
}

#[test]
fn rejects_a_missing_schema_file() {
    let vs = fixture_violations();
    assert!(
        vs.iter().any(
            |v| matches!(v, Violation::SchemaFileMissing { result, schema }
            if result == "MissingFileOne" && schema == "absent.schema.json")
        ),
        "{vs:#?}"
    );
}

#[test]
fn rejects_a_schema_that_does_not_compile() {
    let vs = fixture_violations();
    assert!(
        vs.iter()
            .any(|v| matches!(v, Violation::SchemaInvalid { result, .. }
            if result == "InvalidOne")),
        "{vs:#?}"
    );
}

#[test]
fn rejects_an_entry_with_no_version() {
    let vs = fixture_violations();
    assert!(
        vs.iter()
            .any(|v| matches!(v, Violation::MissingVersion { result } if result == "NoVersionOne")),
        "{vs:#?}"
    );
}

#[test]
fn rejects_a_version_that_disagrees_with_the_schema_id() {
    let vs = fixture_violations();
    assert!(
        vs.iter().any(
            |v| matches!(v, Violation::VersionMismatch { result, entry_version, id_version, .. }
            if result == "MismatchOne" && *entry_version == 1 && *id_version == 2)
        ),
        "{vs:#?}"
    );
}

#[test]
fn rejects_a_malformed_manifest_entry() {
    let vs = fixture_violations();
    assert!(
        vs.iter()
            .any(|v| matches!(v, Violation::MalformedManifestEntry { .. })),
        "{vs:#?}"
    );
}

#[test]
fn rejects_an_undeclared_raw_emit_json_site() {
    let vs = fixture_violations();
    assert!(
        vs.iter().any(
            |v| matches!(v, Violation::UnexpectedRawEmitter { file, expected, found }
            if file == "drifted.rs" && *expected == 0 && *found == 1)
        ),
        "{vs:#?}"
    );
}
