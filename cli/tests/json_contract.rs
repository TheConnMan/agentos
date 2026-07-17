//! Integration: the agent-facing `--json` outputs must validate against the
//! committed JSON Schemas (ADR-0021 decision 1, AC 1 and AC 4). The schema
//! files under `cli/schema/` and the `status_json`/`eval_json` builders do not
//! exist yet, so this file will not compile / the schema loads fail at red; the
//! implementer creates the schemas alongside the `--json` wiring.

use agentos::commands::{eval_json, status_json};
use agentos::evals::CaseOutcome;
use agentos::exit;
use agentos::message::{
    message_awaiting_approval_json, message_dry_run_json, message_reply_json, message_timeout_json,
};
use agentos::observability::{local_endpoints, Endpoint, ObservabilityOutput};
use agentos::ui::{CliOutput, DryRunPlan};
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
    // Two cases, one pass one fail: (id, outcome, seconds, output) rows plus the
    // roll-up. The failing case carries a non-empty reply for diagnosis (#548).
    let results = vec![
        (
            "case-pass".to_string(),
            CaseOutcome::Pass,
            1.5_f64,
            "the answer is 4".to_string(),
        ),
        (
            "case-fail".to_string(),
            CaseOutcome::Fail,
            0.25_f64,
            "i do not know".to_string(),
        ),
    ];
    let value = eval_json(&results);
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "eval_json output must validate against eval.schema.json: {value}"
    );
}

/// The non-graded row is the new contract surface (ADR-0055, #612/#606): it must
/// validate, report `outcome: "plumbing_ok"` with a NULL `passed`, and land in
/// its own roll-up count rather than being folded into passed or failed.
///
/// Deleting the tri-state (making `passed` a bare bool) fails the null assert;
/// deriving `failed` as `total - passed` again fails the `failed` assert, which
/// is the false red R1 rejected.
#[test]
fn a_plumbing_ok_row_validates_and_is_neither_passed_nor_failed() {
    let schema = load_schema("eval.schema.json");
    let results = vec![(
        "case-plumbing".to_string(),
        CaseOutcome::PlumbingOk,
        0.5_f64,
        "all done".to_string(),
    )];
    let value = eval_json(&results);
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "a plumbing_ok row must validate against eval.schema.json: {value}"
    );
    assert_eq!(value["plumbing_ok"], 1, "{value}");
    assert_eq!(
        value["failed"], 0,
        "a non-graded row is not a failure; `failed` must be counted, not derived: {value}"
    );
    assert_eq!(value["cases"][0]["outcome"], "plumbing_ok", "{value}");
    assert!(
        value["cases"][0]["passed"].is_null(),
        "a non-graded row claims neither verdict: {value}"
    );
}

/// The roll-up partitions the rows: every case lands in exactly one of the three
/// counts. A mixed run is where a naive `total - passed` or a plumbing row
/// silently folded into `passed` would show up.
#[test]
fn the_eval_rollup_partitions_every_row_across_the_three_outcomes() {
    let schema = load_schema("eval.schema.json");
    let results = vec![
        (
            "p".to_string(),
            CaseOutcome::Pass,
            1.0_f64,
            "right".to_string(),
        ),
        (
            "f".to_string(),
            CaseOutcome::Fail,
            1.0_f64,
            "wrong".to_string(),
        ),
        (
            "k".to_string(),
            CaseOutcome::PlumbingOk,
            1.0_f64,
            "all done".to_string(),
        ),
    ];
    let value = eval_json(&results);
    assert!(validator(&schema).is_valid(&value), "{value}");
    assert_eq!(value["total"], 3, "{value}");
    assert_eq!(value["passed"], 1, "{value}");
    assert_eq!(
        value["failed"], 1,
        "only the graded failure counts: {value}"
    );
    assert_eq!(value["plumbing_ok"], 1, "{value}");
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
fn message_timeout_json_validates_against_message_schema() {
    let schema = load_schema("message.schema.json");
    let v = validator(&schema);
    let timed_out = message_timeout_json();
    assert!(
        v.is_valid(&timed_out),
        "message_timeout_json must validate against message.schema.json: {timed_out}"
    );
    // Pin the timeout shape: null reply, finalized false, timed_out true.
    assert!(
        timed_out["reply"].is_null(),
        "timeout reply must be JSON null: {timed_out}"
    );
    assert_eq!(timed_out["finalized"], serde_json::json!(false));
    assert_eq!(timed_out["timed_out"], serde_json::json!(true));
}

#[test]
fn message_dry_run_json_validates_against_message_schema() {
    let schema = load_schema("message.schema.json");
    let v = validator(&schema);
    // Explicit channel (local target).
    let with_channel = message_dry_run_json(
        "local",
        "agentos:turns",
        Some("C123"),
        "http://localhost:8155/api/",
    );
    assert!(
        v.is_valid(&with_channel),
        "message_dry_run_json (with channel) must validate: {with_channel}"
    );
    assert_eq!(with_channel["dry_run"], serde_json::json!(true));
    assert_eq!(with_channel["target"], serde_json::json!("local"));
    assert_eq!(with_channel["channel"], serde_json::json!("C123"));
    // Null channel (cluster target, sole-agent resolution).
    let no_channel = message_dry_run_json(
        "cluster",
        "agentos:turns",
        None,
        "http://10.1.2.3:8155/api/",
    );
    assert!(
        v.is_valid(&no_channel),
        "message_dry_run_json (no channel) must validate: {no_channel}"
    );
    assert!(
        no_channel["channel"].is_null(),
        "omitted channel must be JSON null: {no_channel}"
    );
    assert_eq!(no_channel["target"], serde_json::json!("cluster"));
}

#[test]
fn message_schema_variants_are_mutually_exclusive() {
    // The schema is a oneOf; each builder's output must match exactly one variant.
    let schema = load_schema("message.schema.json");
    let v = validator(&schema);
    for value in [
        message_reply_json("1700000000.000100", Some("hi")),
        message_reply_json("1700000000.000100", None),
        message_awaiting_approval_json("1700000000.000100", Some("awaiting approval")),
        message_awaiting_approval_json("1700000000.000100", None),
        message_timeout_json(),
        message_dry_run_json("local", "s", Some("C1"), "http://x/api/"),
    ] {
        assert!(
            v.is_valid(&value),
            "each builder output must satisfy the oneOf: {value}"
        );
    }
}

#[test]
fn message_awaiting_approval_json_validates_and_is_distinct() {
    // #529: the awaiting-approval object is finalized:false + awaiting_approval:true,
    // a distinct terminal state from a reply or a timeout.
    let schema = load_schema("message.schema.json");
    let v = validator(&schema);
    let awaiting = message_awaiting_approval_json("1700000000.000100", Some("card text"));
    assert!(
        v.is_valid(&awaiting),
        "awaiting-approval must validate against message.schema.json: {awaiting}"
    );
    assert_eq!(awaiting["finalized"], serde_json::json!(false));
    assert_eq!(awaiting["awaiting_approval"], serde_json::json!(true));
    assert_eq!(awaiting["reply"], serde_json::json!("card text"));
    assert_eq!(awaiting["thread"], serde_json::json!("1700000000.000100"));
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
fn observability_json_validates_against_observability_schema() {
    let schema = load_schema("observability.schema.json");
    let v = validator(&schema);
    // Both row shapes must validate: a browsable row (url set, note null) and a
    // degraded row (url null, note set).
    let value = ObservabilityOutput::Surfaces(vec![
        Endpoint {
            name: "AgentOS Console".to_string(),
            url: Some("http://localhost:28080/?api=1".to_string()),
            note: None,
            browsable: true,
        },
        Endpoint {
            name: "AgentOS API".to_string(),
            url: None,
            note: Some("service agentos-ui not found".to_string()),
            browsable: false,
        },
    ])
    .to_json();
    assert!(
        v.is_valid(&value),
        "ObservabilityOutput::to_json must validate against observability.schema.json: {value}"
    );
    // Pin the values, not just the types: a degraded row must never smuggle its
    // message into `url`, or an agent cannot parse `url` as a URL.
    assert!(value["surfaces"][1]["url"].is_null());
    assert_eq!(
        value["surfaces"][1]["note"],
        serde_json::json!("service agentos-ui not found")
    );
}

#[test]
fn local_endpoints_json_validates_against_observability_schema() {
    // The real local-tier payload (not a hand-built fixture) must satisfy the
    // committed schema -- this is what `local observability --json` emits.
    let schema = load_schema("observability.schema.json");
    let value = ObservabilityOutput::Surfaces(local_endpoints()).to_json();
    let v = validator(&schema);
    assert!(
        v.is_valid(&value),
        "local_endpoints payload must validate against observability.schema.json: {value}"
    );
}

#[test]
fn observability_dry_run_json_validates_against_observability_schema() {
    // The `--dry-run` branch (cluster tier only) must validate against the SAME
    // committed schema that documents `cluster observability --dry-run --json`
    // -- a consumer validating all `cluster observability --json` output against
    // one schema must not have a legitimate invocation rejected. Built through
    // the real DryRunPlan::to_json, not a hand-written literal, so this test
    // cannot drift from what the command actually emits.
    let schema = load_schema("observability.schema.json");
    let v = validator(&schema);
    let value = ObservabilityOutput::DryRun(DryRunPlan {
        lines: vec![
            "kubectl get pods -n agentos".to_string(),
            "helm get values agentos".to_string(),
        ],
    })
    .to_json();
    assert!(
        v.is_valid(&value),
        "ObservabilityOutput::DryRun must validate against observability.schema.json: {value}"
    );
    // Pin the values, not just the types: dry_run must be the literal true and
    // plan must pass the lines through verbatim.
    assert_eq!(value["dry_run"], serde_json::json!(true));
    assert_eq!(
        value["plan"],
        serde_json::json!(["kubectl get pods -n agentos", "helm get values agentos"])
    );
}

#[test]
fn observability_schema_gate_has_teeth() {
    // negative control: proves the schema gate discriminates
    let schema = load_schema("observability.schema.json");
    let mut value = ObservabilityOutput::Surfaces(vec![Endpoint {
        name: "AgentOS Console".to_string(),
        url: Some("http://localhost:28080/?api=1".to_string()),
        note: None,
        browsable: true,
    }])
    .to_json();
    // Strip a required per-row key; a schema with real teeth must now reject.
    value["surfaces"][0]
        .as_object_mut()
        .expect("each surface row is a JSON object")
        .remove("browsable");
    let v = validator(&schema);
    assert!(
        !v.is_valid(&value),
        "observability schema must reject a row missing the required `browsable` key"
    );
}

#[test]
fn eval_schema_gate_has_teeth() {
    // negative control: proves the schema gate discriminates
    let schema = load_schema("eval.schema.json");
    let results = vec![(
        "only".to_string(),
        CaseOutcome::Pass,
        1.0_f64,
        "ok".to_string(),
    )];
    let mut value = eval_json(&results);
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
