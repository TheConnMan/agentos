//! Field-parity comparator for the `--json` gate (issue #691).
//!
//! Pure, side-effect-free core of the gate: given the text of a Rust source
//! (`cli/src/api.rs` on the real tree, a fixture otherwise), a parsed OpenAPI
//! doc, and a parsed `api-mirrors.json` manifest, return every way a mirror
//! struct has drifted from the API model it declares it mirrors. The gate test
//! (`cli/tests/api_field_parity.rs`) asserts on the returned `Violation`
//! variants; this module never prints or panics on drift.
//!
//! Design notes (kept here, out of the terse return):
//!
//! * The struct inventory is EVERY `Deserialize`-deriving struct in the source,
//!   found via a `syn::visit::Visit` walk. `visit` (not a flat `File.items`
//!   loop) is what reaches structs declared inside fn / impl-method bodies — the
//!   real tree has exactly one, `serde::Deserialize struct BundleFiles` inside
//!   `ApiClient::bundle_files` (`cli/src/api.rs:751-755`). The walk recognizes
//!   both the bare `Deserialize` and qualified `serde::Deserialize` derive
//!   spellings (last path segment == `Deserialize`), and deliberately does NOT
//!   match `Serialize`.
//! * Wire name governs coverage, not the Rust ident: `#[serde(rename="x")]`,
//!   container `#[serde(rename_all=...)]` are applied; `#[serde(skip)]` drops the
//!   field from the wire; `#[serde(alias=...)]` is an ADDITIONAL accepted name so
//!   it never counts as coverage; `#[serde(default)]` / `skip_serializing_if` are
//!   irrelevant to coverage and ignored.
//! * Fail-closed shapes: a `#[serde(flatten)]` field or an object-level
//!   `allOf`/`anyOf`/`oneOf` schema cannot be decomposed field-by-field, so the
//!   struct yields `UnsupportedShape` and is NOT read as zero-required (which
//!   would silently pass).
//! * An omission entry always suppresses `MissingField` for its field; a
//!   dishonest omission (blank/missing `why`, or a field the struct actually
//!   carries, or a field the schema no longer defines) additionally yields
//!   `StaleOmission`.

use std::collections::BTreeSet;

use serde_json::Value;
use syn::visit::Visit;

/// A single way a mirror struct has drifted from its declared API schema. The
/// gate test matches on these variants and their payload, never on messages.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Violation {
    /// Schema defines a property the struct neither carries nor allowlists.
    MissingField {
        struct_name: String,
        schema: String,
        field: String,
    },
    /// Struct carries a wire field the schema does not define.
    UnknownField {
        struct_name: String,
        schema: String,
        field: String,
    },
    /// A `Deserialize` struct in the source is in neither `mirrors` nor `non_mirrors`.
    UndeclaredStruct { struct_name: String },
    /// A manifest entry names a struct the source walk never found.
    StructNotFound { struct_name: String },
    /// A dishonest omission: struct actually carries the field, the schema no
    /// longer has it, or the omission's `why` is blank/missing.
    StaleOmission {
        struct_name: String,
        schema: String,
        field: String,
    },
    /// A manifest `schema` is absent from `components.schemas`.
    SchemaNotFound { struct_name: String, schema: String },
    /// The schema (object-level `allOf`/`anyOf`) or the struct (a
    /// `#[serde(flatten)]` field) cannot be decomposed field-by-field.
    UnsupportedShape { struct_name: String, schema: String },
    /// The inventory holds more than one `Deserialize` struct with this bare
    /// name; the manifest keys by name, so the second is never field-checked.
    DuplicateStruct { struct_name: String },
    /// A manifest entry is missing a required key (`struct`/`schema`), so its
    /// struct would silently escape field comparison.
    MalformedManifestEntry { detail: String },
}

/// One `Deserialize` struct recovered from the source: its bare name, the set of
/// wire field names (rename/rename_all/skip applied), and whether it has a
/// `#[serde(flatten)]` field (which makes it undecomposable).
struct CollectedStruct {
    name: String,
    wire_fields: BTreeSet<String>,
    has_flatten: bool,
}

/// serde attributes distilled from a field's (or container's) `#[serde(...)]`
/// attrs. Only the knobs that affect wire-name coverage are captured.
#[derive(Default)]
struct SerdeAttrs {
    rename: Option<String>,
    rename_all: Option<String>,
    skip: bool,
    flatten: bool,
}

fn parse_serde_attrs(attrs: &[syn::Attribute]) -> SerdeAttrs {
    let mut out = SerdeAttrs::default();
    for attr in attrs {
        if !attr.path().is_ident("serde") {
            continue;
        }
        // Errors are ignored: any serde attr shape this codebase does not use
        // would simply leave `out` at its earlier state; the real tree only uses
        // rename / rename_all / skip / flatten / default / skip_serializing_if /
        // alias, all handled below.
        let _ = attr.parse_nested_meta(|meta| {
            if meta.path.is_ident("skip") || meta.path.is_ident("skip_deserializing") {
                // `skip` drops the field from BOTH directions; `skip_deserializing`
                // drops it from the wire (the decoder populates from Default), so it
                // does NOT cover its schema property either. `skip_serializing`
                // (below, ignored) is still deserialized and DOES cover.
                out.skip = true;
            } else if meta.path.is_ident("flatten") {
                out.flatten = true;
            } else if meta.path.is_ident("rename") {
                let lit: syn::LitStr = meta.value()?.parse()?;
                out.rename = Some(lit.value());
            } else if meta.path.is_ident("rename_all") {
                let lit: syn::LitStr = meta.value()?.parse()?;
                out.rename_all = Some(lit.value());
            } else if meta.input.peek(syn::Token![=]) {
                // Any other value-bearing key (default = "..", alias = "..",
                // skip_serializing_if = "..") — consume and ignore. `alias`
                // deliberately does NOT count as coverage.
                let _: syn::Expr = meta.value()?.parse()?;
            }
            Ok(())
        });
    }
    out
}

/// Does any `#[derive(...)]` on this item name `Deserialize` (bare or
/// `serde::Deserialize`)? Matches on the last path segment, so `Serialize` never
/// counts.
fn derives_deserialize(attrs: &[syn::Attribute]) -> bool {
    for attr in attrs {
        if !attr.path().is_ident("derive") {
            continue;
        }
        let mut found = false;
        let _ = attr.parse_nested_meta(|meta| {
            if let Some(seg) = meta.path.segments.last() {
                if seg.ident == "Deserialize" {
                    found = true;
                }
            }
            Ok(())
        });
        if found {
            return true;
        }
    }
    false
}

/// serde's `rename_all` word-splitting is on the snake_case field ident.
fn apply_rename_all(rule: &str, ident: &str) -> String {
    // Rust fields arrive snake_case; split on `_` into words.
    let words: Vec<&str> = ident.split('_').filter(|w| !w.is_empty()).collect();
    let capitalize = |w: &str| {
        let mut c = w.chars();
        match c.next() {
            Some(f) => f.to_ascii_uppercase().to_string() + &c.as_str().to_ascii_lowercase(),
            None => String::new(),
        }
    };
    match rule {
        "lowercase" => ident.to_ascii_lowercase(),
        "UPPERCASE" => ident.to_ascii_uppercase(),
        "PascalCase" => words.iter().map(|w| capitalize(w)).collect(),
        "camelCase" => {
            let mut it = words.iter();
            let first = it
                .next()
                .map(|w| w.to_ascii_lowercase())
                .unwrap_or_default();
            first + &it.map(|w| capitalize(w)).collect::<String>()
        }
        "snake_case" => words.join("_"),
        "SCREAMING_SNAKE_CASE" => words
            .iter()
            .map(|w| w.to_ascii_uppercase())
            .collect::<Vec<_>>()
            .join("_"),
        "kebab-case" => words.join("-"),
        "SCREAMING-KEBAB-CASE" => words
            .iter()
            .map(|w| w.to_ascii_uppercase())
            .collect::<Vec<_>>()
            .join("-"),
        // Unknown rule: leave the ident untouched rather than guess.
        _ => ident.to_string(),
    }
}

/// Walks a parsed Rust file collecting every `Deserialize`-deriving struct,
/// including those nested in fn / impl-method bodies (the reason this is a
/// `Visit` walk and not a `File.items` loop).
#[derive(Default)]
struct StructCollector {
    structs: Vec<CollectedStruct>,
}

impl<'ast> Visit<'ast> for StructCollector {
    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        if derives_deserialize(&node.attrs) {
            let container = parse_serde_attrs(&node.attrs);
            let mut wire_fields = BTreeSet::new();
            let mut has_flatten = false;
            if let syn::Fields::Named(named) = &node.fields {
                for field in &named.named {
                    let fs = parse_serde_attrs(&field.attrs);
                    if fs.flatten {
                        has_flatten = true;
                        continue;
                    }
                    if fs.skip {
                        continue;
                    }
                    let Some(ident) = field.ident.as_ref() else {
                        continue;
                    };
                    let ident = ident.to_string();
                    let wire = if let Some(rename) = fs.rename {
                        rename
                    } else if let Some(rule) = &container.rename_all {
                        apply_rename_all(rule, &ident)
                    } else {
                        ident
                    };
                    wire_fields.insert(wire);
                }
            }
            self.structs.push(CollectedStruct {
                name: node.ident.to_string(),
                wire_fields,
                has_flatten,
            });
        }
        // Continue the default traversal so structs nested inside THIS struct's
        // context (and, at the file level, inside fn/impl bodies) are still seen.
        syn::visit::visit_item_struct(self, node);
    }
}

fn walk_structs(rust_src: &str) -> Vec<CollectedStruct> {
    let file = match syn::parse_file(rust_src) {
        Ok(f) => f,
        // A source we cannot parse yields no structs; the real-tree test would
        // then fail loudly on the manifest's dangling entries (StructNotFound),
        // which is the correct fail-closed signal.
        Err(_) => return Vec::new(),
    };
    let mut collector = StructCollector::default();
    collector.visit_file(&file);
    collector.structs
}

/// Extracts `e[key]` as a wire string, e.g. a manifest entry's declared
/// struct/schema name. Reused for the declared-name set, the malformed-entry
/// scan, the StructNotFound scan, and the per-mirror field comparison.
fn entry_str<'a>(e: &'a Value, key: &str) -> Option<&'a str> {
    e.get(key).and_then(|v| v.as_str())
}

/// Property names of a schema object, empty if it declares none.
fn schema_props(schema: &Value) -> BTreeSet<String> {
    schema
        .get("properties")
        .and_then(|p| p.as_object())
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default()
}

/// An object-level composition keyword the gate refuses to decompose.
fn is_composed_schema(schema: &Value) -> bool {
    schema.get("allOf").is_some() || schema.get("anyOf").is_some() || schema.get("oneOf").is_some()
}

/// Compare the mirror structs in `rust_src` against the schemas they declare in
/// `manifest`, using `openapi`'s `components.schemas`. Pure: same inputs, same
/// output, so the fixtures drive it identically to the real tree.
pub fn violations(rust_src: &str, openapi: &Value, manifest: &Value) -> Vec<Violation> {
    let mut out = Vec::new();

    let structs = walk_structs(rust_src);
    let found_names: BTreeSet<&str> = structs.iter().map(|s| s.name.as_str()).collect();

    // DuplicateStruct: the manifest keys by bare struct name, so two same-named
    // `Deserialize` structs (legal Rust — e.g. two fn-body-local structs) collapse
    // to one manifest key and only the FIRST is ever field-checked. Fail closed:
    // flag each duplicated name once.
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut flagged_dup: BTreeSet<&str> = BTreeSet::new();
    for s in &structs {
        let name = s.name.as_str();
        if !seen.insert(name) && flagged_dup.insert(name) {
            out.push(Violation::DuplicateStruct {
                struct_name: name.to_string(),
            });
        }
    }

    let mirrors: &[Value] = manifest
        .get("mirrors")
        .and_then(|m| m.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let non_mirrors: &[Value] = manifest
        .get("non_mirrors")
        .and_then(|m| m.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&[]);

    // Every declared struct name, across both lists.
    let declared: BTreeSet<String> = mirrors
        .iter()
        .chain(non_mirrors.iter())
        .filter_map(|e| entry_str(e, "struct").map(String::from))
        .collect();

    // MalformedManifestEntry: a `mirrors` entry lacking `struct` or `schema`, or a
    // `non_mirrors` entry lacking `struct`, has no valid key. Such an entry would
    // otherwise be silently skipped in field comparison (and, if it has a struct,
    // suppress that struct's UndeclaredStruct), letting the struct escape checking.
    // Fail closed: flag it.
    for m in mirrors {
        let has_struct = entry_str(m, "struct");
        let has_schema = entry_str(m, "schema");
        if has_struct.is_none() || has_schema.is_none() {
            let detail = match (has_struct, has_schema) {
                (Some(st), None) => format!("mirrors entry for struct {st:?} missing schema"),
                (None, Some(sc)) => format!("mirrors entry missing struct (schema {sc:?})"),
                _ => "mirrors entry missing struct and schema".to_string(),
            };
            out.push(Violation::MalformedManifestEntry { detail });
        }
    }
    for e in non_mirrors {
        if entry_str(e, "struct").is_none() {
            out.push(Violation::MalformedManifestEntry {
                detail: "non_mirrors entry missing struct".to_string(),
            });
        }
    }

    // UndeclaredStruct: an inventoried struct in neither list (D2's teeth).
    for s in &structs {
        if !declared.contains(&s.name) {
            out.push(Violation::UndeclaredStruct {
                struct_name: s.name.clone(),
            });
        }
    }

    // StructNotFound: a manifest entry (either list) naming a struct the walk
    // never found — the fail-closed twin that makes a walk defect observable.
    for entry in mirrors.iter().chain(non_mirrors.iter()) {
        if let Some(name) = entry_str(entry, "struct") {
            if !found_names.contains(name) {
                out.push(Violation::StructNotFound {
                    struct_name: name.to_string(),
                });
            }
        }
    }

    let empty_schemas = serde_json::Map::new();
    let schemas = openapi
        .get("components")
        .and_then(|c| c.get("schemas"))
        .and_then(|s| s.as_object())
        .unwrap_or(&empty_schemas);

    // Field comparison, per mirror entry.
    for m in mirrors {
        let Some(struct_name) = entry_str(m, "struct") else {
            continue;
        };
        let Some(schema_name) = entry_str(m, "schema") else {
            continue;
        };
        // No struct found for this entry -> already reported as StructNotFound.
        let Some(s) = structs.iter().find(|s| s.name.as_str() == struct_name) else {
            continue;
        };

        let Some(schema) = schemas.get(schema_name) else {
            out.push(Violation::SchemaNotFound {
                struct_name: struct_name.to_string(),
                schema: schema_name.to_string(),
            });
            continue;
        };

        if s.has_flatten {
            out.push(Violation::UnsupportedShape {
                struct_name: struct_name.to_string(),
                schema: schema_name.to_string(),
            });
            continue;
        }
        if is_composed_schema(schema) {
            out.push(Violation::UnsupportedShape {
                struct_name: struct_name.to_string(),
                schema: schema_name.to_string(),
            });
            continue;
        }

        let props = schema_props(schema);
        let wire = &s.wire_fields;

        // Validate omissions. An omission always suppresses MissingField for its
        // field; a dishonest one additionally yields StaleOmission.
        let mut suppressed: BTreeSet<String> = BTreeSet::new();
        if let Some(omissions) = m.get("omissions").and_then(|o| o.as_array()) {
            for om in omissions {
                let Some(field) = om.get("field").and_then(|v| v.as_str()) else {
                    continue;
                };
                suppressed.insert(field.to_string());
                let why = om.get("why").and_then(|v| v.as_str()).unwrap_or("").trim();
                let dishonest = why.is_empty()      // blank/missing justification
                    || wire.contains(field)         // struct actually carries it
                    || !props.contains(field); // schema no longer defines it
                if dishonest {
                    out.push(Violation::StaleOmission {
                        struct_name: struct_name.to_string(),
                        schema: schema_name.to_string(),
                        field: field.to_string(),
                    });
                }
            }
        }

        // MissingField: a schema property neither carried nor suppressed.
        for p in &props {
            if !wire.contains(p) && !suppressed.contains(p) {
                out.push(Violation::MissingField {
                    struct_name: struct_name.to_string(),
                    schema: schema_name.to_string(),
                    field: p.clone(),
                });
            }
        }

        // UnknownField: a wire field with no schema property behind it. No
        // allowlist path — a CLI field with no API field is always a bug.
        for w in wire {
            if !props.contains(w) {
                out.push(Violation::UnknownField {
                    struct_name: struct_name.to_string(),
                    schema: schema_name.to_string(),
                    field: w.clone(),
                });
            }
        }
    }

    out
}
