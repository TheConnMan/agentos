//! Unit: eval concurrency forward-surface + single-node detection (#706).
//!
//! Real parallelism is worker-side and deferred to #709; the CLI eval loop
//! stays sequential. What #706 delivers CLI-side is a forward-compatible
//! `--concurrency` flag that REFUSES any value above 1 (never silently
//! accepts and runs sequentially anyway), plus a pure schedulable-node-count
//! helper used to enrich the per-case timeout diagnostic with a
//! single-node-saturation hint.
//!
//! These tests pin two pure functions the implementer will add to
//! `cli/src/message.rs` (imported here from the `curie` lib), plus a new
//! `pub concurrency: usize` field on `EvalOpts`:
//!
//!   pub fn resolve_eval_concurrency(requested: usize) -> anyhow::Result<usize>
//!       Ok(1) when requested == 1; Err(..) for any requested > 1, with an
//!       error message containing "#709" and "not yet supported".
//!
//!   pub fn schedulable_node_count(nodes_json: &str) -> usize
//!       Parses `kubectl get nodes -o json` stdout; counts nodes that are
//!       Ready AND not cordoned (`spec.unschedulable != true`).
//!
//! Until both exist (and `EvalOpts` gains `concurrency`), this test target
//! fails to compile: that is the intended RED, isolated to this file because
//! it imports from the lib rather than adding inline lib tests. The lib
//! itself still compiles.

use curie::message::{resolve_eval_concurrency, schedulable_node_count};

/// The refusal seam, negative case: any requested concurrency above the
/// sequential default of 1 must be refused (implement-or-explicitly-decline),
/// never silently downgraded to sequential without telling the caller.
#[test]
fn refuses_concurrency_above_one() {
    let err = resolve_eval_concurrency(2)
        .expect_err("requesting concurrency > 1 must be refused, not silently run sequentially");
    let message = err.to_string();
    assert!(
        message.contains("#709"),
        "error must name the follow-up issue #709 that will implement real concurrency: {message:?}"
    );
    assert!(
        message.contains("not yet supported"),
        "error must say concurrency is not yet supported: {message:?}"
    );
}

/// The refusal seam, positive case: the sequential default is always accepted.
#[test]
fn accepts_sequential_concurrency() {
    assert_eq!(
        resolve_eval_concurrency(1).expect("concurrency 1 (sequential) must be accepted"),
        1
    );
}

/// `0` is not a valid concurrency (there is no such thing as running zero
/// cases at a time), so it must be refused rather than silently normalized to
/// sequential -- accepting it would make a `--dry-run` plan print the nonsense
/// "sequential (0)" (issue #706).
#[test]
fn refuses_zero_concurrency() {
    let err = resolve_eval_concurrency(0)
        .expect_err("requesting concurrency 0 must be refused, not silently run sequentially");
    let message = err.to_string();
    assert!(
        message.contains('0'),
        "error should reference the invalid value 0: {message:?}"
    );
}

/// Single-node fixture, mirroring real `kubectl get nodes -o json` shape: one
/// Ready, schedulable node.
const SINGLE_NODE_JSON: &str = r#"{
  "items": [
    {
      "metadata": {"name": "node-1"},
      "spec": {},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    }
  ]
}"#;

#[test]
fn schedulable_node_count_single_node() {
    assert_eq!(schedulable_node_count(SINGLE_NODE_JSON), 1);
}

/// Three nodes: two Ready-and-schedulable, one cordoned
/// (`spec.unschedulable == true`). The cordoned node must be excluded from
/// the schedulable count even though it is Ready.
const MULTI_NODE_WITH_CORDON_JSON: &str = r#"{
  "items": [
    {
      "metadata": {"name": "node-1"},
      "spec": {},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    },
    {
      "metadata": {"name": "node-2"},
      "spec": {"unschedulable": true},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    },
    {
      "metadata": {"name": "node-3"},
      "spec": {},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    }
  ]
}"#;

#[test]
fn schedulable_node_count_multi_and_cordoned() {
    assert_eq!(schedulable_node_count(MULTI_NODE_WITH_CORDON_JSON), 2);
}

/// No items at all (an empty cluster listing, or a malformed/absent `items`
/// key) must count as zero, not panic.
const EMPTY_NODES_JSON: &str = r#"{"items": []}"#;

#[test]
fn schedulable_node_count_empty_or_zero() {
    assert_eq!(schedulable_node_count(EMPTY_NODES_JSON), 0);
}

/// A node whose `status.conditions` Ready entry reads `status: "False"` is
/// NotReady and must not count, even though it is not cordoned. Mixed with a
/// genuinely Ready node so the count also proves the good node was not
/// dropped by mistake.
const NOT_READY_NODE_JSON: &str = r#"{
  "items": [
    {
      "metadata": {"name": "node-1"},
      "spec": {},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    },
    {
      "metadata": {"name": "node-2"},
      "spec": {},
      "status": {"conditions": [{"type": "Ready", "status": "False"}]}
    }
  ]
}"#;

#[test]
fn schedulable_node_count_excludes_not_ready() {
    assert_eq!(schedulable_node_count(NOT_READY_NODE_JSON), 1);
}

/// Malformed / non-JSON stdout (a probe failure, truncated output, or
/// `kubectl` printing an error string instead of JSON) must yield 0 rather
/// than panic -- a parse failure should never masquerade as a healthy
/// multi-node cluster.
#[test]
fn schedulable_node_count_malformed_input_returns_zero_without_panicking() {
    assert_eq!(schedulable_node_count("not json at all"), 0);
    assert_eq!(schedulable_node_count(""), 0);
    assert_eq!(schedulable_node_count("{\"items\": \"not-an-array\"}"), 0);
}
