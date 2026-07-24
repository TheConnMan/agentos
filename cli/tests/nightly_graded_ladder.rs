//! ADR-0081 / issue #872: the nightly graded parity ladder is the workflow
//! that runs the cold-start parity ladder (`curie dev e2e-ladder`) LIVE
//! against a real model, not the sealed fake-model install `ci.yaml` runs on
//! every PR. `ci.yaml`'s cluster ladder job installs with `--fake-model`
//! (ADR-0055 bounds what that fake green means: plumbing only, never a graded
//! turn). The nightly workflow sits on the opposite side of that seam: it
//! must arm `CURIE_E2E_LIVE`, never seal the install, and carry the
//! OpenRouter credential only as the one env key the runner reads.
//!
//! Grounding for the OpenRouter/env-key claims asserted below:
//! - `docs/interfaces/model-provider/INTERFACE.md:73-76`: an `sk-or-` credential
//!   auto-selects `OPENROUTER_BASE_URL`; no `ANTHROPIC_BASE_URL` is set for this
//!   provider, and the real key travels as `ANTHROPIC_API_KEY` inside the
//!   runner -- the CLI-facing input for that credential is `CURIE_CREDENTIALS`.
//! - `cli/src/ops.rs:529-532`: `--allow-egress-host` takes the provider
//!   KEYWORD `openrouter` (resolved to `openrouter.ai` at install time), never
//!   a bare hostname like `openrouter.ai` itself.
//!
//! This file is a text-contract test against the workflow YAML AS TEXT (no
//! `serde_yaml`, no new dependency -- std `fs` only), so it fails today
//! because `.github/workflows/nightly-graded-ladder.yaml` does not exist yet.
//! Each assertion targets a user-visible CI contract (arms live, opens
//! egress, never seals, never leaks the secret), not Rust internals, so it
//! survives a rename or reformat of the workflow as long as the contract
//! holds. Deleting the workflow fails every assertion below except the CI
//! sibling anchor (assertion group 3), which stays green against the
//! existing `ci.yaml` and only breaks if someone arms `ci.yaml`'s fake seal
//! off, proving the two workflows are pinned to opposite sides of the seam.

use std::fs;
use std::path::PathBuf;

/// Read a workflow file's raw text, or an empty string when it does not exist
/// yet. Assertions on an empty string fail with their own readable messages
/// rather than panicking on a missing file, so a missing nightly workflow
/// surfaces as a normal test failure naming the violated contract.
fn workflow_text(name: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../.github/workflows")
        .join(name);
    fs::read_to_string(path).unwrap_or_default()
}

fn nightly() -> String {
    workflow_text("nightly-graded-ladder.yaml")
}

fn ci() -> String {
    workflow_text("ci.yaml")
}

fn count_lines_containing(text: &str, needle: &str) -> usize {
    text.lines().filter(|line| line.contains(needle)).count()
}

// --- Assertion group 1: arms the GRADED path -------------------------------

/// The nightly workflow must arm live grading with the exact double-quoted
/// form, never a bare or unquoted `1` that a YAML parser could read as an
/// integer instead of the string the ladder script compares against.
#[test]
fn nightly_arms_live_grading_with_the_exact_quoted_form() {
    let text = nightly();
    assert!(
        text.contains("CURIE_E2E_LIVE: \"1\""),
        "the nightly workflow must arm CURIE_E2E_LIVE with the exact quoted \
         form `CURIE_E2E_LIVE: \"1\"` so the ladder runs live, not fake; \
         file contents:\n{text}"
    );
}

/// Every line referencing the OpenRouter secret must also assign
/// `CURIE_CREDENTIALS:` on that same line, so the secret reaches the ladder
/// only through the one env key the CLI reads (never bare, never aliased to
/// a differently-named env var that could accidentally land on a `run:` line
/// elsewhere).
#[test]
fn nightly_secret_reaches_the_ladder_only_as_curie_credentials() {
    let text = nightly();
    assert!(
        text.contains("secrets.OPENROUTER_API_KEY"),
        "the nightly workflow must reference secrets.OPENROUTER_API_KEY at \
         least once to supply the live model credential; file contents:\n{text}"
    );
    for line in text.lines() {
        if line.contains("secrets.OPENROUTER_API_KEY") {
            assert!(
                line.contains("CURIE_CREDENTIALS:"),
                "every line referencing secrets.OPENROUTER_API_KEY must also \
                 assign CURIE_CREDENTIALS: on that same line, so the secret \
                 reaches the ladder only as that one env key: {line}"
            );
        }
    }
}

/// The model default `z-ai/glm-5.2` must appear on a line that also names
/// `CURIE_MODEL`, so the graded default is wired as the model the ladder
/// actually calls, not just mentioned in a comment.
#[test]
fn nightly_wires_the_glm_default_model_via_curie_model() {
    let text = nightly();
    let has_model_line = text
        .lines()
        .any(|line| line.contains("z-ai/glm-5.2") && line.contains("CURIE_MODEL"));
    assert!(
        has_model_line,
        "the nightly workflow must have a line containing both the model id \
         z-ai/glm-5.2 and the CURIE_MODEL env key, wiring the default model \
         into the ladder; file contents:\n{text}"
    );
}

/// The nightly ladder must cover both tier sets: the fast `skill,local` rungs
/// and the separate `cluster` rung. Accept either quoted or unquoted YAML
/// scalars by checking substrings rather than exact-matching the whole line.
#[test]
fn nightly_covers_both_the_skill_local_and_cluster_tier_sets() {
    let text = nightly();
    assert!(
        text.contains("skill,local"),
        "the nightly workflow must set CURIE_E2E_TIERS to a value containing \
         `skill,local` so the fast rungs run graded too; file contents:\n{text}"
    );
    let has_cluster_tiers = text.lines().any(|line| {
        let normalized: String = line.split_whitespace().collect();
        normalized.contains("CURIE_E2E_TIERS:cluster")
            || normalized.contains("CURIE_E2E_TIERS:\"cluster\"")
    });
    assert!(
        has_cluster_tiers,
        "the nightly workflow must have a CURIE_E2E_TIERS: cluster (quoted or \
         unquoted) line covering the cluster rung separately from \
         skill,local; file contents:\n{text}"
    );
}

// --- Assertion group 2: cluster graded install -----------------------------

/// The cluster install must open egress to the `openrouter` provider keyword
/// (resolved to `openrouter.ai` at install time by `cli/src/ops.rs:529-532`),
/// and the workflow must never contain the sealed-install flag anywhere --
/// proving the cluster rung is graded, not fake.
#[test]
fn nightly_cluster_install_opens_openrouter_egress_and_never_seals() {
    let text = nightly();
    assert!(
        text.contains("--allow-egress-host openrouter"),
        "the nightly workflow's cluster install must open egress with \
         `--allow-egress-host openrouter` (the provider keyword, not a bare \
         hostname) so the graded model call is reachable; file contents:\n{text}"
    );
    assert!(
        !text.contains("--fake-model"),
        "the nightly workflow must never seal the install with --fake-model \
         anywhere in the file (including comments); a sealed install cannot \
         be the graded ladder; file contents:\n{text}"
    );
}

// --- Assertion group 3: sibling / negative parity (mandatory) --------------

/// `ci.yaml`'s cluster ladder job DOES seal its install with `--fake-model`.
/// This is the sibling anchor: it pins the two workflows on opposite sides of
/// the fake/graded seam. This assertion passes today against the existing
/// `ci.yaml` and fails only if someone arms ci.yaml graded (removing its
/// `--fake-model`) or de-arms the nightly ladder, collapsing the seam this
/// whole test file exists to guard.
#[test]
fn ci_yaml_still_seals_its_cluster_install_with_fake_model() {
    let text = ci();
    assert!(
        text.contains("--fake-model"),
        "ci.yaml must still contain --fake-model in its cluster ladder job; \
         if this ever fails, either ci.yaml was accidentally armed graded or \
         the fake/graded seam this test guards has collapsed; file \
         contents:\n{text}"
    );
}

// --- Assertion group 4: #632 secret posture --------------------------------

/// The workflow must declare least-privilege `permissions:` with
/// `contents: read`, so the nightly job cannot write back to the repo.
#[test]
fn nightly_declares_least_privilege_contents_read_permissions() {
    let text = nightly();
    assert!(
        text.contains("permissions:"),
        "the nightly workflow must declare a permissions: block (least \
         privilege, #632); file contents:\n{text}"
    );
    assert!(
        text.contains("contents: read"),
        "the nightly workflow's permissions: block must include \
         `contents: read`; file contents:\n{text}"
    );
}

/// Every `actions/checkout` use must be paired with
/// `persist-credentials: false`, so the checkout-injected token cannot
/// override a differently-scoped credential used later in the job (the same
/// class of hazard the checkout-credentials learning documents). Counting
/// occurrences (rather than requiring exact adjacency) keeps the assertion
/// robust to step reordering while still catching a checkout step that lacks
/// the setting entirely.
#[test]
fn nightly_pairs_every_checkout_with_persist_credentials_false() {
    let text = nightly();
    let checkout_count = count_lines_containing(&text, "uses: actions/checkout");
    assert!(
        checkout_count > 0,
        "the nightly workflow must use actions/checkout at least once; file \
         contents:\n{text}"
    );
    let persist_false_count = count_lines_containing(&text, "persist-credentials: false");
    assert!(
        persist_false_count >= checkout_count,
        "every actions/checkout use ({checkout_count}) must be paired with \
         persist-credentials: false ({persist_false_count} found); a bare \
         checkout leaves the job token in global git config for later steps \
         to pick up; file contents:\n{text}"
    );
}

/// The OpenRouter secret must never be echoed on a `run:` line. Combined with
/// assertion group 1's "only as CURIE_CREDENTIALS:" check, this closes off
/// the one remaining way the secret could leak into job logs.
#[test]
fn nightly_never_echoes_the_openrouter_secret_on_a_run_line() {
    let text = nightly();
    for line in text.lines() {
        if line.contains("secrets.OPENROUTER_API_KEY") {
            assert!(
                !line.contains("run:"),
                "the OpenRouter secret must never appear on a `run:` line \
                 (it would be echoed into job logs): {line}"
            );
        }
    }
}
