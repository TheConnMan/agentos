//! Stream B tests for `agentos skill check` (issue #337): the offline,
//! credential-free MCP load check.
//!
//! These pin the frozen Section-3 runner<->CLI JSON seam and the docker argv
//! for the one-shot check container. They are written test-first: the public
//! API they reference (`docker::CheckSpec`, `commands::parse_check_report`,
//! `commands::check_outcome`, `commands::CheckReport`) does not exist yet, so
//! this binary fails to compile until the Implementer adds it. That RED state
//! is the intended contract handoff.

use agentos::commands::{check_outcome, parse_check_report};
use agentos::docker::CheckSpec;
use agentos::exit::ExitClass;

/// A realistic green seam payload (Section 3): declared server registered as a
/// connected, plugin-owned (`scope: "dynamic"`) server with tools.
const GREEN_JSON: &str = r#"{
  "check": "mcp-load",
  "version": 1,
  "plugin_dir": "/plugin",
  "declared": [
    { "name": "text-stats-engine", "source": "plugin.json", "form": "inline" }
  ],
  "registered": [
    {
      "name": "plugin:text-stats-engine:text-stats-engine",
      "status": "connected",
      "tools": ["count_words", "top_words"],
      "error": null,
      "scope": "dynamic"
    }
  ],
  "matches": [
    { "declared": "text-stats-engine", "registered": "plugin:text-stats-engine:text-stats-engine", "connected": true, "tool_count": 2 }
  ],
  "verdict": "green",
  "reasons": [],
  "hints": []
}"#;

/// A realistic red seam payload (Section 3): the #336 string-pointer form, whose
/// declared server never registers. `reasons` non-empty; the inline-object
/// fingerprint rides in `hints`.
const RED_JSON: &str = r#"{
  "check": "mcp-load",
  "version": 1,
  "plugin_dir": "/plugin",
  "declared": [
    { "name": "text-stats-engine", "source": "plugin.json", "form": "string_pointer" }
  ],
  "registered": [],
  "matches": [
    { "declared": "text-stats-engine", "registered": null, "connected": false, "tool_count": 0 }
  ],
  "verdict": "red",
  "reasons": [
    "declared text-stats-engine never registered",
    "declared 1 MCP server(s); none registered with tools"
  ],
  "hints": [
    "plugin.json 'mcpServers' is a string pointer; the real loader silently ignores this form — inline the object"
  ]
}"#;

/// The verbatim runner shape: `registered[].tools` items are SDK `McpToolInfo`
/// OBJECTS (`{"name": ..., "annotations"?: ...}`), NOT strings. This is the real
/// contract the runner emits; it must parse and map to Ok.
const GREEN_OBJECT_TOOLS_JSON: &str = r#"{
  "check": "mcp-load",
  "version": 1,
  "plugin_dir": "/plugin",
  "declared": [
    { "name": "mcp-green", "source": "plugin.json", "form": "inline" }
  ],
  "registered": [
    {
      "name": "plugin:mcp-green:green-probe",
      "status": "connected",
      "serverInfo": { "name": "mcp-green-probe", "version": "1.28.1" },
      "scope": "dynamic",
      "tools": [ { "name": "word_count", "annotations": {} } ]
    }
  ],
  "matches": [
    { "declared": "mcp-green", "registered": "plugin:mcp-green:green-probe", "connected": true, "tool_count": 1 }
  ],
  "verdict": "green",
  "reasons": [],
  "hints": []
}"#;

/// A realistic invalid-bundle seam payload (Section 3): the bundle dir exists
/// but fails structural `plugin_format` validation, which the runner reports as
/// `verdict: "invalid_bundle"` with the validation errors in `reasons`. The CLI
/// must map this to a Usage error (exit 2), matching the runner's own exit 2.
const INVALID_BUNDLE_JSON: &str = r#"{
  "check": "mcp-load",
  "version": 1,
  "plugin_dir": "/plugin",
  "declared": [],
  "registered": [],
  "matches": [],
  "verdict": "invalid_bundle",
  "reasons": [
    "skills/: no SKILL.md found for declared skill 'text-stats'",
    ".claude-plugin/plugin.json: 'name' is required"
  ],
  "hints": []
}"#;

/// A future-version payload the CLI must hard-fail on (Section 7(e)).
const VERSION_2_JSON: &str = r#"{
  "check": "mcp-load",
  "version": 2,
  "plugin_dir": "/plugin",
  "declared": [],
  "registered": [],
  "matches": [],
  "verdict": "green",
  "reasons": [],
  "hints": []
}"#;

// --- Test 6: CheckSpec::run_args exact argv --------------------------------

#[test]
fn check_run_args_are_the_exact_offline_argv() {
    let spec = CheckSpec {
        image: "agentos-runner".into(),
        plugin_dir: "/tmp/deal-desk".into(),
        timeout_s: 30,
    };
    let args = spec.run_args();

    // The argv MUST be exactly this vector, in this order.
    let expected: Vec<String> = [
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        "/tmp/deal-desk:/plugin:ro",
        "-e",
        "AGENTOS_PLUGIN_DIR=/plugin",
        "-e",
        "AGENTOS_CHECK_TIMEOUT_S=30",
        "agentos-runner",
        "python",
        "-m",
        "agentos_runner.check",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(
        args, expected,
        "check run_args drifted from the frozen argv"
    );

    let joined = args.join(" ");
    // Offline contract: the check container is network-isolated.
    assert!(
        joined.contains("--network none"),
        "check must run with --network none (offline contract)"
    );
    // Read-only bundle mount.
    assert!(joined.contains("-v /tmp/deal-desk:/plugin:ro"));
    // The timeout is plumbed through as the container deadline.
    assert!(joined.contains("-e AGENTOS_CHECK_TIMEOUT_S=30"));
    // The CMD override runs check mode, not the ACI session entrypoint.
    assert!(joined.ends_with("python -m agentos_runner.check"));

    // Absence: check mode is not an ACI session and carries no credentials.
    assert!(!joined.contains("-p "), "check must not publish a port");
    assert!(
        !args.iter().any(|a| a == "-p"),
        "check must not publish a port"
    );
    assert!(
        !joined.contains("--name"),
        "check is one-shot, no container name"
    );
    assert!(!joined.contains("AGENTOS_SESSION_ID"));
    assert!(!joined.contains("AGENTOS_SANDBOX_ID"));
    assert!(!joined.contains("AGENTOS_BUDGET"));
    assert!(!joined.contains("AGENTOS_FAKE_MODEL"));
    // No credential env of any kind (spike-verified credential-free connect).
    assert!(!joined.contains("ANTHROPIC"));
    assert!(!joined.contains("CLAUDE_CODE_OAUTH_TOKEN"));
    assert!(!joined.contains("API_KEY"));
}

#[test]
fn check_run_args_carry_the_specced_timeout() {
    let spec = CheckSpec {
        image: "ghcr.io/example/agentos-runner:1.2.3".into(),
        plugin_dir: "/work/bundle".into(),
        timeout_s: 45,
    };
    let joined = spec.run_args().join(" ");
    assert!(joined.contains("-e AGENTOS_CHECK_TIMEOUT_S=45"));
    assert!(joined.contains("-v /work/bundle:/plugin:ro"));
    assert!(joined.contains("ghcr.io/example/agentos-runner:1.2.3"));
}

// --- Test 7: verdict-JSON -> outcome mapping -------------------------------

#[test]
fn green_report_parses_and_maps_to_ok() {
    let report = parse_check_report(GREEN_JSON).expect("green seam JSON parses");
    assert_eq!(report.version, 1);
    assert_eq!(report.verdict, "green");
    assert!(report.reasons.is_empty(), "green carries no reasons");

    check_outcome(&report).expect("a green verdict maps to Ok(())");
}

#[test]
fn green_report_with_object_tools_parses_and_maps_to_ok() {
    // Guards the real runner seam: `tools` items are McpToolInfo objects, not
    // strings. `registered` is opaque pass-through JSON, so any tool/server
    // shape parses cleanly (the whole tools-shape bug class is eliminated).
    let report =
        parse_check_report(GREEN_OBJECT_TOOLS_JSON).expect("object-tools seam JSON parses");
    assert_eq!(report.version, 1);
    assert_eq!(report.verdict, "green");
    assert_eq!(report.registered.len(), 1);
    assert!(
        !report.registered.is_empty(),
        "the object-shaped registered server round-trips through opaque JSON"
    );

    check_outcome(&report).expect("a green verdict with object tools maps to Ok(())");
}

#[test]
fn red_report_maps_to_failure_with_a_fix_hint() {
    let report = parse_check_report(RED_JSON).expect("red seam JSON parses");
    assert_eq!(report.verdict, "red");
    assert!(!report.reasons.is_empty(), "red must carry reasons");

    let err = check_outcome(&report).expect_err("a red verdict maps to Err");
    assert_eq!(
        err.class,
        ExitClass::Failure,
        "red is a plain Failure (exit 1)"
    );
    let fix = err
        .fix
        .expect("red outcome must carry an actionable fix hint");
    assert!(!fix.trim().is_empty(), "the fix hint must be non-empty");
}

#[test]
fn invalid_bundle_report_maps_to_usage_exit_2_with_reasons() {
    let report = parse_check_report(INVALID_BUNDLE_JSON).expect("invalid_bundle seam JSON parses");
    assert_eq!(report.verdict, "invalid_bundle");
    assert!(
        !report.reasons.is_empty(),
        "invalid_bundle must carry structural reasons"
    );

    let err = check_outcome(&report).expect_err("an invalid_bundle verdict maps to Err");
    assert_eq!(
        err.class,
        ExitClass::Usage,
        "invalid_bundle is a Usage error (exit 2), matching the runner's exit 2"
    );
    // The structural validation errors must surface in the message so the user
    // sees WHY the bundle is invalid.
    assert!(
        err.message
            .contains("no SKILL.md found for declared skill 'text-stats'"),
        "the message must carry the structural reasons, got: {}",
        err.message
    );
    let fix = err
        .fix
        .expect("invalid_bundle outcome must carry an actionable fix hint");
    assert!(!fix.trim().is_empty(), "the fix hint must be non-empty");
}

#[test]
fn version_mismatch_is_a_parse_error_naming_the_contract() {
    let err = parse_check_report(VERSION_2_JSON)
        .expect_err("a version != 1 payload must hard-fail the parse");
    let msg = err.to_string().to_lowercase();
    assert!(
        msg.contains("version") || msg.contains("contract"),
        "the version-drift error must name the version/contract, got: {msg}"
    );
}

#[test]
fn garbage_stdout_is_a_parse_error() {
    parse_check_report("this is not json at all {{{")
        .expect_err("unparseable stdout must be an error, not a silent green");
}
