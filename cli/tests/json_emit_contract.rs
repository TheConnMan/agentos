//! Integration: the `--json` emit contract (issue #456, ADR-0021). Under
//! `--json`, EVERY agent-facing verb must emit exactly one JSON object to
//! stdout -- never empty stdout. Today the read verbs and the whole `--dry-run`
//! surface go through `ui.payload`/`ui.kv`/`ui.payload_plain`, which suppress
//! under `--json`, so those verbs emit empty stdout + exit 0 (the bug). These
//! tests are RED now (empty stdout, no JSON object) and GREEN once the handlers
//! route their payload through a `--json` sink that always emits an object.
//!
//! The dry-run coverage is manifest-driven on purpose: it walks the committed
//! `command-manifest.json`, so a verb added later with a `--dry-run` arg is
//! auto-covered without touching this test. Exit codes are deliberately ignored
//! (comms/eval legitimately exit non-zero with a JSON *error* object); the bug
//! under test is empty stdout, not a nonzero code.

use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn manifest() -> serde_json::Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/command-manifest.json");
    let raw = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("committed manifest {path} must exist: {e}"));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("manifest {path} must be valid JSON: {e}"))
}

/// Placeholder value for a required arg. `--limit` parses as an f64, so a
/// non-numeric placeholder would be a clap parse error (empty stdout -> false
/// failure); everything else is a plain string.
fn placeholder(id: &str) -> &'static str {
    match id {
        "limit" => "1",
        _ => "x",
    }
}

/// Every LEAF verb path (e.g. `["cluster", "kill"]`) whose `args` include an arg
/// with `"id": "dry_run"`, paired with the argv fragment of its required-arg
/// placeholders (positional value, or `--<long>` + value for a required flag).
fn dry_run_verbs() -> Vec<(Vec<String>, Vec<String>)> {
    let mut out = Vec::new();
    fn args_of(node: &serde_json::Value) -> &[serde_json::Value] {
        node.get("args")
            .and_then(|a| a.as_array())
            .map_or(&[], |a| a.as_slice())
    }
    fn walk(
        node: &serde_json::Value,
        path: Vec<String>,
        out: &mut Vec<(Vec<String>, Vec<String>)>,
    ) {
        let subs = node
            .get("subcommands")
            .and_then(|s| s.as_array())
            .filter(|s| !s.is_empty());
        match subs {
            Some(subs) => {
                for sub in subs {
                    let name = sub
                        .get("name")
                        .and_then(|n| n.as_str())
                        .expect("subcommand has a name")
                        .to_string();
                    let mut child = path.clone();
                    child.push(name);
                    walk(sub, child, out);
                }
            }
            None => {
                // Leaf verb: collect it only if it carries a --dry-run arg.
                let args = args_of(node);
                let has_dry = args
                    .iter()
                    .any(|a| a.get("id").and_then(|i| i.as_str()) == Some("dry_run"));
                if !has_dry {
                    return;
                }
                let mut required = Vec::new();
                for a in args {
                    if a.get("required").and_then(|r| r.as_bool()) != Some(true) {
                        continue;
                    }
                    let id = a.get("id").and_then(|i| i.as_str()).unwrap_or("");
                    if a.get("positional").and_then(|p| p.as_bool()) == Some(true) {
                        required.push(placeholder(id).to_string());
                    } else {
                        let long = a
                            .get("long")
                            .and_then(|l| l.as_str())
                            .expect("required non-positional arg has a --long");
                        required.push(format!("--{long}"));
                        required.push(placeholder(id).to_string());
                    }
                }
                out.push((path, required));
            }
        }
    }
    walk(&manifest(), Vec::new(), &mut out);
    out
}

#[test]
fn every_dry_run_verb_emits_json_object() {
    let verbs = dry_run_verbs();
    // Guard against a vacuously-passing empty walk, and against a manifest that
    // silently drops verbs: ~24 carry --dry-run today, so a loose floor of 20.
    assert!(
        verbs.len() >= 20,
        "expected >= 20 --dry-run verbs in the manifest, found {} ({:?}); \
         a vacuous or shrunken walk must fail loudly",
        verbs.len(),
        verbs.iter().map(|(p, _)| p.join(" ")).collect::<Vec<_>>()
    );

    for (path, required) in &verbs {
        let mut argv: Vec<String> = path.clone();
        argv.extend(required.iter().cloned());
        argv.push("--dry-run".to_string());
        argv.push("--json".to_string());

        // cargo runs integration tests with cwd = `cli/`, where `charts/agentos`
        // and the compose file don't resolve -- so the cluster/local operator
        // verbs (`up`/`down`/`status`) would error on chart resolution and never
        // reach their real dry-run branch, satisfying "non-empty JSON" via the
        // centralized *error* path instead of the *plan* path (a gate hole).
        // Run from the worktree root so those verbs hit their true dry-run
        // branch; pre-fix that path emits exit 0 + empty stdout (the RED we want).
        let root = concat!(env!("CARGO_MANIFEST_DIR"), "/..");
        let output = Command::new(bin())
            .current_dir(root)
            .args(&argv)
            .output()
            .unwrap_or_else(|e| panic!("run agentos {}: {e}", argv.join(" ")));
        let stdout = String::from_utf8_lossy(&output.stdout);

        assert!(
            !stdout.trim().is_empty(),
            "`agentos {}` under --json must not emit empty stdout\nstderr: {}",
            argv.join(" "),
            String::from_utf8_lossy(&output.stderr)
        );
        let parsed: serde_json::Value =
            serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
                panic!(
                    "`agentos {}` under --json must emit parseable JSON: {e}\nstdout: {stdout}",
                    argv.join(" ")
                )
            });
        assert!(
            parsed.is_object(),
            "`agentos {}` under --json must emit a JSON object, got: {stdout}",
            argv.join(" ")
        );
    }
}

#[test]
fn observability_emits_json_object() {
    // AC1: `local observability` has no --dry-run, so the manifest walk misses
    // it. It is network-free and prints three URLs (Console, Langfuse, API
    // base); nothing opens in a browser unless `--open` is passed, and `--json`
    // never opens one regardless (see `should_open`). This is a canary: its
    // assertions are intentionally left unmodified by #460 so a passing run
    // proves the local `--json` shape stayed a strict superset of the shipped
    // `{"surfaces":[{"name","url"}]}` contract (empty-stdout was already fixed
    // by #456/PR#503, before this branch).
    let output = Command::new(bin())
        .args(["local", "observability", "--json"])
        .output()
        .expect("run agentos local observability --json");
    let stdout = String::from_utf8_lossy(&output.stdout);

    assert!(
        !stdout.trim().is_empty(),
        "`agentos local observability --json` must not emit empty stdout\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|e| panic!("must emit parseable JSON: {e}\nstdout: {stdout}"));
    assert!(
        parsed.is_object(),
        "`agentos local observability --json` must emit a JSON object, got: {stdout}"
    );
    assert_eq!(
        output.status.code(),
        Some(0),
        "observability is hermetic and must exit 0\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn dry_run_json_masks_credentials() {
    // SECURITY: the new JSON sink must carry the ALREADY-MASKED plan string, not
    // a raw secret. The non-json dry-run masks the model credential via
    // `CmdArg::SecretSet` (first 8 chars + "***"), so `sk-ant-SECRETSENTINEL9999`
    // prints as `sk-ant-S***` and the raw sentinel never appears. The --json
    // output must preserve exactly that masking. RED now = empty stdout.
    //
    // `--chart` is required: a dev build with no `charts/agentos` in the test
    // binary's cwd (the `cli/` package dir) errors before building the plan, so
    // point it at the committed chart at the worktree root (one level up from
    // CARGO_MANIFEST_DIR). `--dry-run` never fetches or stats it.
    let chart = concat!(env!("CARGO_MANIFEST_DIR"), "/../charts/agentos");
    let output = Command::new(bin())
        .args(["cluster", "up", "--chart", chart, "--dry-run", "--json"])
        .env("AGENTOS_MODEL_CREDENTIALS", "sk-ant-SECRETSENTINEL9999")
        .output()
        .expect("run agentos cluster up --dry-run --json");
    let stdout = String::from_utf8_lossy(&output.stdout);

    assert!(
        !stdout.trim().is_empty(),
        "`agentos cluster up --dry-run --json` must not emit empty stdout\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|e| panic!("must emit parseable JSON: {e}\nstdout: {stdout}"));
    assert!(
        parsed.is_object(),
        "`agentos cluster up --dry-run --json` must emit a JSON object, got: {stdout}"
    );
    assert!(
        stdout.contains("sk-ant-S***"),
        "the JSON plan must carry the masked credential marker `sk-ant-S***`: {stdout}"
    );
    assert!(
        !stdout.contains("SECRETSENTINEL9999"),
        "the raw model credential must NEVER appear in the JSON output: {stdout}"
    );
}

// ---------------------------------------------------------------------------
// The `eval --dry-run` PLAN path (not the error path)
// ---------------------------------------------------------------------------

/// The manifest-driven gate above drives every `--dry-run` verb with NO required
/// args beyond placeholders, so `eval` fails cases-resolution and satisfies the
/// "emits an object" assertion via the CENTRALIZED ERROR-JSON path -- it never
/// reaches its dry-run PLAN branch, which is the branch that was actually broken.
/// This drives `eval` with a real `--cases` file so the plan branch is exercised,
/// and asserts the payload is the PLAN (no `error` key), so an error-path
/// regression cannot green this test. `--dry-run` touches no network.
fn assert_eval_dry_run_plan(tier: &str) {
    let output = Command::new(bin())
        .args([
            tier,
            "eval",
            "--cases",
            "examples/weather/evals/cases.json",
            "--dry-run",
            "--json",
        ])
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .output()
        .unwrap_or_else(|e| panic!("run agentos {tier} eval --dry-run --json: {e}"));
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(
        output.status.code(),
        Some(0),
        "`agentos {tier} eval --dry-run --json` is hermetic and must exit 0\nstdout: {stdout}\nstderr: {stderr}"
    );
    assert!(
        !stdout.trim().is_empty(),
        "`agentos {tier} eval --dry-run --json` must not emit empty stdout\nstderr: {stderr}"
    );
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|e| panic!("must emit parseable JSON: {e}\nstdout: {stdout}"));
    assert!(
        parsed.is_object(),
        "`agentos {tier} eval --dry-run --json` must emit a JSON object, got: {stdout}"
    );
    assert!(
        parsed.get("error").is_none(),
        "must be the dry-run PLAN, not an error object -- the false-green this gap was about: {stdout}"
    );
    assert_eq!(
        parsed.get("dry_run"),
        Some(&serde_json::Value::Bool(true)),
        "the plan object must carry `dry_run: true`: {stdout}"
    );
    let plan = parsed
        .get("plan")
        .and_then(|p| p.as_array())
        .unwrap_or_else(|| panic!("`plan` must be an array: {stdout}"));
    assert!(
        !plan.is_empty(),
        "`plan` must be a NON-empty array of the lines eval would run: {stdout}"
    );
    let first = plan[0].as_str().unwrap_or_default();
    assert!(
        first.contains("weather"),
        "the first plan line must name the resolved suite (`weather`), proving cases resolved: {first:?}"
    );
}

#[test]
fn local_eval_dry_run_emits_plan_not_error() {
    assert_eval_dry_run_plan("local");
}

#[test]
fn cluster_eval_dry_run_emits_plan_not_error() {
    assert_eval_dry_run_plan("cluster");
}

// ---------------------------------------------------------------------------
// The `comms --dry-run` PLAN path (not the error path)
// ---------------------------------------------------------------------------

/// Sentinel Slack tokens. Distinctive enough that a raw leak anywhere in stdout
/// is unambiguous, and long enough to survive the `xapp-SEN***` masking prefix.
const SENTINEL_APP_TOKEN: &str = "xapp-SENTINELAPP1234";
const SENTINEL_BOT_TOKEN: &str = "xoxb-SENTINELBOT5678";

/// Drives a `<tier> comms` variant with `--dry-run --json` and asserts the
/// payload is the PLAN, returning it for the caller's tier-specific checks.
///
/// The manifest-driven gate above supplies NO provider flags, so `comms` fails
/// its `require_provider`/`require_connect_tokens` check and satisfies the
/// "emits an object" assertion via the CENTRALIZED ERROR-JSON path -- it never
/// reaches its dry-run PLAN branch, which is the branch that was actually
/// broken. These drive real connect/disconnect inputs so the plan branch is
/// exercised, and the `no error key` assertion means an error-path regression
/// cannot green them. Both tiers emit the plan and return BEFORE their
/// `require_on_path("helm"/"kubectl"/"docker")` calls, so this is hermetic:
/// no network, no cluster, no tooling on PATH.
fn assert_comms_dry_run_plan(tier: &str, extra: &[&str]) -> serde_json::Value {
    let mut args = vec![tier, "comms", "--slack"];
    args.extend_from_slice(extra);
    args.extend_from_slice(&["--dry-run", "--json"]);
    let output = Command::new(bin())
        .args(&args)
        // The token flags default from these; remove them so an ambient shell
        // value can never influence the plan under test.
        .env_remove("SLACK_APP_TOKEN")
        .env_remove("SLACK_BOT_TOKEN")
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .output()
        .unwrap_or_else(|e| panic!("run agentos {args:?}: {e}"));
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert_eq!(
        output.status.code(),
        Some(0),
        "`agentos {args:?}` is hermetic and must exit 0\nstdout: {stdout}\nstderr: {stderr}"
    );
    assert!(
        !stdout.trim().is_empty(),
        "`agentos {args:?}` must not emit empty stdout\nstderr: {stderr}"
    );
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|e| panic!("must emit parseable JSON: {e}\nstdout: {stdout}"));
    assert!(
        parsed.is_object(),
        "`agentos {args:?}` must emit a JSON object, got: {stdout}"
    );
    assert!(
        parsed.get("error").is_none(),
        "must be the dry-run PLAN, not an error object -- the false-green this gap was about: {stdout}"
    );
    assert_eq!(
        parsed.get("dry_run"),
        Some(&serde_json::Value::Bool(true)),
        "the plan object must carry `dry_run: true`: {stdout}"
    );
    let plan = parsed
        .get("plan")
        .and_then(|p| p.as_array())
        .unwrap_or_else(|| panic!("`plan` must be an array: {stdout}"));
    assert!(
        !plan.is_empty(),
        "`plan` must be a NON-empty array of the lines comms would run: {stdout}"
    );
    parsed
}

/// Reinforces the masking contract: comms tokens go through `CmdArg::SecretSet`,
/// so `cmd.display()` renders them as a `xapp-SEN***` prefix. A regression that
/// swapped `SecretSet` for a plain arg would spill the raw token into the plan
/// -- and into any agent's logs that captured this `--json` payload.
fn assert_tokens_masked(stdout: &str) {
    assert!(
        !stdout.contains(SENTINEL_APP_TOKEN),
        "the raw app token must NEVER appear in the plan: {stdout}"
    );
    assert!(
        !stdout.contains(SENTINEL_BOT_TOKEN),
        "the raw bot token must NEVER appear in the plan: {stdout}"
    );
    assert!(
        stdout.contains("xapp-SEN***"),
        "the app token must appear MASKED (`xapp-SEN***`), proving it was carried as a secret rather than dropped: {stdout}"
    );
    assert!(
        stdout.contains("xoxb-SEN***"),
        "the bot token must appear MASKED (`xoxb-SEN***`): {stdout}"
    );
}

#[test]
fn local_comms_dry_run_emits_plan_not_error() {
    let parsed = assert_comms_dry_run_plan(
        "local",
        &[
            "--app-token",
            SENTINEL_APP_TOKEN,
            "--bot-token",
            SENTINEL_BOT_TOKEN,
        ],
    );
    let stdout = parsed.to_string();
    assert_tokens_masked(&stdout);
    let first = parsed["plan"][0].as_str().unwrap_or_default();
    assert!(
        first.contains("docker compose") && first.contains("agentos-dispatcher"),
        "the local plan must be the compose connect command that brings up the dispatcher: {first:?}"
    );
}

#[test]
fn cluster_comms_dry_run_emits_plan_not_error() {
    let parsed = assert_comms_dry_run_plan(
        "cluster",
        &[
            "--app-token",
            SENTINEL_APP_TOKEN,
            "--bot-token",
            SENTINEL_BOT_TOKEN,
        ],
    );
    let stdout = parsed.to_string();
    assert_tokens_masked(&stdout);
    let first = parsed["plan"][0].as_str().unwrap_or_default();
    assert!(
        first.contains("helm upgrade") && first.contains("dispatcher.slack.appToken"),
        "the cluster plan must be the helm upgrade that sets the dispatcher's Slack tokens: {first:?}"
    );
}

/// `--disconnect` needs no tokens (`require_connect_tokens` short-circuits), so
/// it reaches the plan branch on both tiers and is worth pinning: it is the
/// variant an agent runs to tear Slack back down.
#[test]
fn local_comms_disconnect_dry_run_emits_plan_not_error() {
    let parsed = assert_comms_dry_run_plan("local", &["--disconnect"]);
    let plan = parsed["plan"].as_array().expect("plan array");
    assert!(
        plan.iter().any(|l| l
            .as_str()
            .unwrap_or_default()
            .contains("stop agentos-dispatcher")),
        "the local disconnect plan must stop the dispatcher: {parsed}"
    );
}

#[test]
fn cluster_comms_disconnect_dry_run_emits_plan_not_error() {
    let parsed = assert_comms_dry_run_plan("cluster", &["--disconnect"]);
    let first = parsed["plan"][0].as_str().unwrap_or_default();
    assert!(
        first.contains("helm upgrade") && first.contains("dispatcher.slack.appToken="),
        "the cluster disconnect plan must clear the dispatcher's Slack tokens: {first:?}"
    );
}

// ---------------------------------------------------------------------------
// `to_json` key-shape pins
// ---------------------------------------------------------------------------
//
// The read verbs' real (`List`) path needs a live server, so the binary-driven
// tests above cannot reach it -- and they only assert `is_object()`, which a
// dropped or renamed key still satisfies. These unit-test `to_json` directly and
// compare against an EXACT literal, so renaming, dropping, OR adding a key fails.
// This is the only coverage of the agent-facing key contract.

use agentos::api::{MemoryEntry, Version};
use agentos::commands::{
    ApprovalsOutput, BudgetOutput, DeleteOutput, KillOutput, MemoryOutput, ResumeOutput,
    VersionsOutput,
};
// `ObservabilityOutput` moved to the tier-aware `observability` seam (#460) so
// both the local and cluster handlers return one type.
use agentos::observability::{Endpoint, ObservabilityOutput};
use agentos::ui::{CliOutput, DryRunPlan};
use serde_json::json;

fn plan(lines: &[&str]) -> DryRunPlan {
    DryRunPlan {
        lines: lines.iter().map(|l| l.to_string()).collect(),
    }
}

#[test]
fn dry_run_plan_json_shape_is_pinned() {
    assert_eq!(
        plan(&["helm upgrade --install agentos", "kubectl rollout status"]).to_json(),
        json!({
            "dry_run": true,
            "plan": ["helm upgrade --install agentos", "kubectl rollout status"],
        })
    );
    // An empty plan still emits the `plan` key as an array, never null/absent.
    assert_eq!(plan(&[]).to_json(), json!({"dry_run": true, "plan": []}));
}

#[test]
fn versions_output_json_shape_is_pinned() {
    assert_eq!(
        VersionsOutput::DryRun(plan(&["GET <api>/agents/<id>/versions"])).to_json(),
        json!({"dry_run": true, "plan": ["GET <api>/agents/<id>/versions"]})
    );
    assert_eq!(
        VersionsOutput::Empty {
            agent: "weather".to_string(),
        }
        .to_json(),
        json!({"agent": "weather", "versions": []})
    );
    // Two versions, one with every optional field `None` and one with them all
    // `Some`, so the null-vs-value rendering of each optional is pinned. `id` is
    // deliberately NOT emitted; adding it would fail this exact-match.
    assert_eq!(
        VersionsOutput::List {
            agent: "weather".to_string(),
            versions: vec![
                Version {
                    id: "ver_1".to_string(),
                    version_label: "v1".to_string(),
                    commit_sha: None,
                    bundle_sha256: None,
                    created_by: None,
                    created_at: None,
                },
                Version {
                    id: "ver_2".to_string(),
                    version_label: "v2".to_string(),
                    commit_sha: Some("abc1234".to_string()),
                    bundle_sha256: Some("deadbeef00".to_string()),
                    created_by: Some("bconn".to_string()),
                    created_at: Some("2026-07-16T00:00:00Z".to_string()),
                },
            ],
        }
        .to_json(),
        json!({
            "agent": "weather",
            "versions": [
                {
                    "version_label": "v1",
                    "commit_sha": null,
                    "bundle_sha256": null,
                    "created_by": null,
                    "created_at": null,
                },
                {
                    "version_label": "v2",
                    "commit_sha": "abc1234",
                    "bundle_sha256": "deadbeef00",
                    "created_by": "bconn",
                    "created_at": "2026-07-16T00:00:00Z",
                },
            ],
        })
    );
}

#[test]
fn memory_output_json_shape_is_pinned() {
    assert_eq!(
        MemoryOutput::DryRun(plan(&["GET <api>/agents/<id>/memory"])).to_json(),
        json!({"dry_run": true, "plan": ["GET <api>/agents/<id>/memory"]})
    );
    assert_eq!(
        MemoryOutput::Empty {
            agent: "weather".to_string(),
        }
        .to_json(),
        json!({"agent": "weather", "entries": []})
    );
    // `MemoryEntry::version` is deliberately NOT emitted; this exact-match pins
    // that (surfacing it later would fail here and force a contract decision).
    assert_eq!(
        MemoryOutput::List {
            agent: "weather".to_string(),
            entries: vec![
                MemoryEntry {
                    index: 0,
                    content: "user prefers celsius".to_string(),
                    version: 3,
                },
                MemoryEntry {
                    index: 1,
                    content: "home airport is BOS".to_string(),
                    version: 3,
                },
            ],
        }
        .to_json(),
        json!({
            "agent": "weather",
            "entries": [
                {"index": 0, "content": "user prefers celsius"},
                {"index": 1, "content": "home airport is BOS"},
            ],
        })
    );
}

#[test]
fn approvals_output_json_shape_is_pinned() {
    assert_eq!(
        ApprovalsOutput::DryRun(plan(&["GET <api>/agents/<id>"])).to_json(),
        json!({"dry_run": true, "plan": ["GET <api>/agents/<id>"]})
    );
    // No gates: `gated_tools` must still be an empty array, never null/absent --
    // an agent consumer branches on the array, not on key presence.
    assert_eq!(
        ApprovalsOutput::Gates {
            agent: "weather".to_string(),
            gated_tools: vec![],
        }
        .to_json(),
        json!({"agent": "weather", "gated_tools": []})
    );
    assert_eq!(
        ApprovalsOutput::Gates {
            agent: "weather".to_string(),
            gated_tools: vec!["Bash".to_string(), "mcp__weather__forecast".to_string()],
        }
        .to_json(),
        json!({
            "agent": "weather",
            "gated_tools": ["Bash", "mcp__weather__forecast"],
        })
    );
}

/// Deliberate, reviewed contract EVOLUTION (#460), not a weakened pin: the
/// payload key `surfaces` and the row key `name` are unchanged, and this still
/// asserts EXACT equality against a literal, so a dropped/renamed/added key
/// fails exactly as before. What changed is that each row is now an additive
/// SUPERSET -- `note` and `browsable` join `name`/`url`. Existing agents reading
/// `surfaces[].name`/`.url` keep working.
///
/// Per the repo convention pinned by `kill_output_json_shape_is_pinned` ("the
/// false case must emit `killed: false`, not omit the key"), all four keys are
/// ALWAYS emitted with explicit nulls -- never conditionally omitted.
#[test]
fn observability_output_json_shape_is_pinned() {
    assert_eq!(
        ObservabilityOutput::Surfaces(vec![
            // Browsable row: url set, note explicitly null.
            Endpoint {
                name: "AgentOS Console".to_string(),
                url: Some("http://localhost:28080/?api=1".to_string()),
                note: None,
                browsable: true,
            },
            Endpoint {
                name: "Langfuse UI".to_string(),
                url: Some("http://localhost:23000".to_string()),
                note: None,
                browsable: true,
            },
            // Non-browsable row WITH a url: the API base is an agent target,
            // never opened in a browser.
            Endpoint {
                name: "AgentOS API".to_string(),
                url: Some("http://localhost:28000".to_string()),
                note: None,
                browsable: false,
            },
            // Degraded row: url explicitly null, note carries the message --
            // never smuggled into `url`.
            Endpoint {
                name: "Langfuse UI".to_string(),
                url: None,
                note: Some("service agentos-langfuse-web not found".to_string()),
                browsable: false,
            },
        ])
        .to_json(),
        json!({
            "surfaces": [
                {"name": "AgentOS Console", "url": "http://localhost:28080/?api=1", "note": null, "browsable": true},
                {"name": "Langfuse UI", "url": "http://localhost:23000", "note": null, "browsable": true},
                {"name": "AgentOS API", "url": "http://localhost:28000", "note": null, "browsable": false},
                {"name": "Langfuse UI", "url": null, "note": "service agentos-langfuse-web not found", "browsable": false},
            ],
        })
    );
}

/// The cluster tier returns the SAME `ObservabilityOutput`, so its payload is
/// pinned at the `to_json` level: `cluster observability` needs a real cluster,
/// so there is no hermetic binary test for it the way `local` has
/// `observability_emits_json_object`. This pins cross-tier payload parity --
/// three surfaces, identical key set, degrading per endpoint rather than
/// hard-failing when a service is missing.
#[test]
fn cluster_observability_output_json_shape_is_pinned() {
    assert_eq!(
        ObservabilityOutput::Surfaces(vec![
            Endpoint {
                name: "AgentOS Console".to_string(),
                url: Some("http://10.0.0.5:31234/?api=1".to_string()),
                note: None,
                browsable: true,
            },
            // A ClusterIP service degrades to a port-forward hint, not a URL.
            Endpoint {
                name: "Langfuse UI".to_string(),
                url: None,
                note: Some(
                    "kubectl -n agentos port-forward svc/agentos-langfuse-web 3000:3000  \
                     then http://localhost:3000"
                        .to_string()
                ),
                browsable: false,
            },
            Endpoint {
                name: "AgentOS API".to_string(),
                url: Some("http://10.0.0.5:31234/api".to_string()),
                note: None,
                browsable: false,
            },
        ])
        .to_json(),
        json!({
            "surfaces": [
                {"name": "AgentOS Console", "url": "http://10.0.0.5:31234/?api=1", "note": null, "browsable": true},
                {"name": "Langfuse UI", "url": null, "note": "kubectl -n agentos port-forward svc/agentos-langfuse-web 3000:3000  then http://localhost:3000", "browsable": false},
                {"name": "AgentOS API", "url": "http://10.0.0.5:31234/api", "note": null, "browsable": false},
            ],
        })
    );
}

#[test]
fn kill_output_json_shape_is_pinned() {
    assert_eq!(
        KillOutput::DryRun(plan(&["POST <api>/agents/<id>/kill"])).to_json(),
        json!({"dry_run": true, "plan": ["POST <api>/agents/<id>/kill"]})
    );
    assert_eq!(
        KillOutput::Done {
            agent: "weather".to_string(),
            killed: true,
        }
        .to_json(),
        json!({"agent": "weather", "killed": true})
    );
    // The false case must emit `killed: false`, not omit the key.
    assert_eq!(
        KillOutput::Done {
            agent: "weather".to_string(),
            killed: false,
        }
        .to_json(),
        json!({"agent": "weather", "killed": false})
    );
}

#[test]
fn resume_output_json_shape_is_pinned() {
    assert_eq!(
        ResumeOutput::DryRun(plan(&["POST <api>/agents/<id>/resume"])).to_json(),
        json!({"dry_run": true, "plan": ["POST <api>/agents/<id>/resume"]})
    );
    assert_eq!(
        ResumeOutput::Done {
            agent: "weather".to_string(),
            killed: false,
        }
        .to_json(),
        json!({"agent": "weather", "killed": false})
    );
    assert_eq!(
        ResumeOutput::Done {
            agent: "weather".to_string(),
            killed: true,
        }
        .to_json(),
        json!({"agent": "weather", "killed": true})
    );
}

#[test]
fn budget_output_json_shape_is_pinned() {
    assert_eq!(
        BudgetOutput::DryRun(plan(&["PATCH <api>/agents/<id>"])).to_json(),
        json!({"dry_run": true, "plan": ["PATCH <api>/agents/<id>"]})
    );
    assert_eq!(
        BudgetOutput::Done {
            agent: "weather".to_string(),
            max_usd_per_day: Some(12.5),
        }
        .to_json(),
        json!({"agent": "weather", "max_usd_per_day": 12.5})
    );
    // `None` means "platform default" and must serialize as an explicit null --
    // omitting the key would read to an agent as "no budget field at all".
    assert_eq!(
        BudgetOutput::Done {
            agent: "weather".to_string(),
            max_usd_per_day: None,
        }
        .to_json(),
        json!({"agent": "weather", "max_usd_per_day": null})
    );
}

#[test]
fn delete_output_json_shape_is_pinned() {
    assert_eq!(
        DeleteOutput::DryRun(plan(&["DELETE <api>/agents/<id>"])).to_json(),
        json!({"dry_run": true, "plan": ["DELETE <api>/agents/<id>"]})
    );
    assert_eq!(
        DeleteOutput::Done {
            agent: "weather".to_string(),
        }
        .to_json(),
        json!({"agent": "weather", "deleted": true})
    );
}

// ---------------------------------------------------------------------------
// The operator + deploy verbs' real-path `to_json` (#485)
// ---------------------------------------------------------------------------
//
// These verbs' success path needs a live server/cluster, so the binary-driven
// tests above cannot reach it. Pin their `to_json` shapes directly so a
// dropped/renamed/added key fails here -- the same discipline the read verbs get.

#[test]
fn deploy_output_json_shape_is_pinned() {
    use agentos::commands::DeployOutput;
    assert_eq!(
        DeployOutput {
            plugin_name: "weather".to_string(),
            label: "v1-123".to_string(),
            env: "dev".to_string(),
            agent_name: "weather".to_string(),
            agent_id: "agt_1".to_string(),
            version_label: "v1-123".to_string(),
            version_id: "ver_1".to_string(),
            channel: "unchanged (C123)".to_string(),
            bundle_ref: "bundles/abc.tar.gz".to_string(),
            bundle_sha256: "deadbeef00".to_string(),
            bundle_size_bytes: 4096,
            deployment_id: "dep_1".to_string(),
            deployment_environment: "dev".to_string(),
            deployment_status: "active".to_string(),
        }
        .to_json(),
        json!({
            "plugin": "weather",
            "label": "v1-123",
            "environment": "dev",
            "agent": {"name": "weather", "id": "agt_1"},
            "version": {"label": "v1-123", "id": "ver_1"},
            "channel": "unchanged (C123)",
            "bundle": {"ref": "bundles/abc.tar.gz", "sha256": "deadbeef00", "size_bytes": 4096},
            "deployment": {"id": "dep_1", "environment": "dev", "status": "active"},
        })
    );
}

#[test]
fn comms_output_json_shape_is_pinned() {
    use agentos::comms::CommsOutput;
    assert_eq!(
        CommsOutput::DryRun(plan(&["helm upgrade"])).to_json(),
        json!({"dry_run": true, "plan": ["helm upgrade"]})
    );
    // The disconnect case must emit `connected: false`, not omit the key.
    assert_eq!(
        CommsOutput::Done { connected: false }.to_json(),
        json!({"connected": false})
    );
    assert_eq!(
        CommsOutput::Done { connected: true }.to_json(),
        json!({"connected": true})
    );
}

#[test]
fn cluster_up_down_output_json_shapes_are_pinned() {
    use agentos::ops::{ClusterDownOutput, ClusterUpOutput};
    assert_eq!(
        ClusterUpOutput::Up {
            namespace: "agentos".to_string(),
            release: "agentos".to_string(),
        }
        .to_json(),
        json!({"status": "up", "namespace": "agentos", "release": "agentos"})
    );
    assert_eq!(
        ClusterDownOutput::Aborted.to_json(),
        json!({"down": false, "aborted": true})
    );
    assert_eq!(
        ClusterDownOutput::Down {
            release_was_absent: false,
        }
        .to_json(),
        json!({"down": true, "release_was_absent": false})
    );
}

#[test]
fn local_operator_output_json_shapes_are_pinned() {
    use agentos::local::{LocalDownOutput, LocalStatusOutput, LocalUpOutput};
    assert_eq!(
        LocalUpOutput::Up {
            endpoints: vec![("Console".to_string(), "http://localhost:28080".to_string())],
            slack: false,
        }
        .to_json(),
        json!({
            "status": "up",
            "endpoints": [{"name": "Console", "url": "http://localhost:28080"}],
            "slack": false,
        })
    );
    assert_eq!(
        LocalStatusOutput::Services {
            rows: vec!["NAME  STATUS".to_string()],
        }
        .to_json(),
        json!({"services": ["NAME  STATUS"]})
    );
    assert_eq!(
        LocalDownOutput::Down {
            volumes_wiped: true,
            reaped: 2,
        }
        .to_json(),
        json!({"stopped": true, "volumes_wiped": true, "runners_reaped": 2})
    );
    assert_eq!(
        LocalDownOutput::Aborted.to_json(),
        json!({"stopped": false, "aborted": true})
    );
}
