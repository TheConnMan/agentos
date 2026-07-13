//! Integration: the agent-facing `--json` outputs must validate against the
//! committed JSON Schemas (ADR-0021 decision 1, AC 1 and AC 4). The schema
//! files under `cli/schema/` and the `status_json`/`eval_json` builders do not
//! exist yet, so this file will not compile / the schema loads fail at red; the
//! implementer creates the schemas alongside the `--json` wiring.

use agentos::commands::{eval_json, status_json};
use agentos::exit;
use agentos::message::message_reply_json;
use agentos_aci_protocol::SessionStatus;

fn load_schema(name: &str) -> serde_json::Value {
    let path = format!("{}/schema/{}", env!("CARGO_MANIFEST_DIR"), name);
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("committed schema {path} must exist: {e}"));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("schema {path} must be valid JSON: {e}"))
}

fn validator(schema: &serde_json::Value) -> jsonschema::Validator {
    jsonschema::validator_for(schema).expect("schema compiles to a validator")
}

#[test]
fn status_json_validates_against_status_schema() {
    let schema = load_schema("status.schema.json");
    let value = status_json("http://127.0.0.1:8787", &SessionStatus::Done);
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "status_json output must validate against status.schema.json: {value}"
    );
}

#[test]
fn eval_json_validates_against_eval_schema() {
    let schema = load_schema("eval.schema.json");
    // Two cases, one pass one fail: (id, passed, seconds) rows plus the roll-up.
    let results = vec![
        ("case-pass".to_string(), true, 1.5_f64),
        ("case-fail".to_string(), false, 0.25_f64),
    ];
    let value = eval_json(&results, 1, 2);
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "eval_json output must validate against eval.schema.json: {value}"
    );
}

#[test]
fn error_json_validates_against_error_schema() {
    let schema = load_schema("error.schema.json");
    let err = exit::usage("x").context("y");
    let value = exit::error_json(&err);
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "error_json output must validate against error.schema.json: {value}"
    );
}

#[test]
fn message_reply_json_validates_against_message_schema() {
    let schema = load_schema("message.schema.json");
    let v = validator(&schema);
    // Replied case: a non-null reply and finalized true.
    let replied = message_reply_json("1700000000.000100", Some("the answer is 42"));
    assert!(
        v.is_valid(&replied),
        "message_reply_json (replied) must validate against message.schema.json: {replied}"
    );
    // Pin the values, not just the types: the reply text must pass through, the
    // thread must echo the input, and finalized must track reply.is_some().
    assert_eq!(replied["reply"], serde_json::json!("the answer is 42"));
    assert_eq!(replied["thread"], serde_json::json!("1700000000.000100"));
    assert_eq!(replied["finalized"], serde_json::json!(true));
    // No-edit completion: reply null, finalized false, must also validate.
    let no_edit = message_reply_json("1700000000.000100", None);
    assert!(
        v.is_valid(&no_edit),
        "message_reply_json (no edit) must validate against message.schema.json: {no_edit}"
    );
    // Pin the no-edit values: null reply, thread passthrough, finalized false.
    assert!(
        no_edit["reply"].is_null(),
        "no-edit reply must be JSON null: {no_edit}"
    );
    assert_eq!(no_edit["thread"], serde_json::json!("1700000000.000100"));
    assert_eq!(no_edit["finalized"], serde_json::json!(false));
}

#[test]
fn message_schema_gate_has_teeth() {
    // negative control: proves the schema gate discriminates
    let schema = load_schema("message.schema.json");
    let mut value = message_reply_json("1700000000.000100", Some("hi"));
    // Strip a required key; a schema with real teeth must now reject.
    value
        .as_object_mut()
        .expect("message_reply_json returns a JSON object")
        .remove("reply");
    let v = validator(&schema);
    assert!(
        !v.is_valid(&value),
        "message schema must reject an object missing the required `reply` key"
    );
}

#[test]
fn eval_schema_gate_has_teeth() {
    // negative control: proves the schema gate discriminates
    let schema = load_schema("eval.schema.json");
    let results = vec![("only".to_string(), true, 1.0_f64)];
    let mut value = eval_json(&results, 1, 1);
    // Strip a required top-level key; a schema with real teeth must now reject.
    value
        .as_object_mut()
        .expect("eval_json returns a JSON object")
        .remove("total");
    let v = validator(&schema);
    assert!(
        !v.is_valid(&value),
        "eval schema must reject an object missing the required `total` key"
    );
}
