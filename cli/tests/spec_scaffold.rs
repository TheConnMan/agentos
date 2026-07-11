//! Integration tests for issue #325: `agentos init --from-spec <PATH>`.
//!
//! These pin the acceptance criteria for scaffolding a Claude Code plugin
//! bundle NON-INTERACTIVELY from an agent-authored spec file, and for the
//! scaffolded `evals/cases.json` being loadable by the SAME loader that
//! `agentos skill eval` uses (`agentos::evals::load_suite`). The parity
//! guarantee is the whole point: a spec authored by an agent must produce a
//! bundle whose eval suite the platform eval path accepts unchanged.
//!
//! Written test-first: the `agentos::spec` module, `scaffold_from_spec`, and
//! the `--from-spec` clap flag do not exist yet, so this file fails to compile
//! and/or fails at runtime until the implementer builds the feature.
//!
//! Note on the fixtures: `instructions` bodies keep their markdown headings off
//! the opening JSON quote (a leading sentence first) so the raw-string literals
//! never contain a `"#` sequence that would prematurely close an `r#"..."#`
//! delimiter. The `\n` escapes are literal in the raw string and parse to real
//! newlines via serde_json, which is what an authored markdown body would carry.

use std::path::Path;
use std::process::Command;

use agentos::evals::{load_suite, GraderKind};
use agentos::scaffold::{read_manifest, scaffold_from_spec};
use agentos::spec::parse;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn output_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

/// A valid two-skill spec with one connector and one eval. The single eval
/// grader is `{kind: contains, expected: "all done"}` deliberately: that is the
/// fake-model-compatible grader that makes the offline `skill eval` parity demo
/// pass, so a spec authored this way scaffolds a suite that greens without a
/// real model.
fn valid_spec_json() -> &'static str {
    r#"{
      "name": "deal-desk",
      "description": "Prices and reviews deal desk requests.",
      "skills": [
        {
          "name": "deal-desk",
          "description": "Invoke when a rep submits a pricing exception request.",
          "allowed_tools": ["WebSearch", "WebFetch"],
          "instructions": "Deal desk skill body.\n\n## When to run\nA rep asks for a pricing exception.\n"
        },
        {
          "name": "renewal-review",
          "description": "Invoke when a renewal needs a discount review.",
          "instructions": "Renewal review skill body.\n\n## When to run\nA renewal needs review.\n"
        }
      ],
      "connectors": {
        "crm": { "command": "crm-mcp", "args": ["--stdio"] }
      },
      "evals": [
        { "id": "prices-a-deal", "input": "Quote 20% off for Acme", "grader": { "kind": "contains", "expected": "all done", "case_sensitive": false } }
      ]
    }"#
}

/// Write `body` to `<dir>/spec.json` and return the path.
fn write_spec(dir: &Path, body: &str) -> std::path::PathBuf {
    let path = dir.join("spec.json");
    std::fs::write(&path, body).expect("write spec fixture");
    path
}

// --- library-level scaffold behavior --------------------------------------

/// The happy path: a valid spec scaffolds every bundle artifact with the
/// content the spec dictates. Deleting the impl (returning no files) or writing
/// the wrong name/description makes concrete file-content assertions fail.
#[test]
fn scaffolds_the_full_bundle_from_a_valid_spec() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(valid_spec_json()).expect("valid spec parses");
    let created = scaffold_from_spec(&out, &spec).expect("scaffold succeeds");
    assert!(!created.is_empty(), "scaffold should report created files");

    // 1. Manifest: name + version from the spec, description carried through.
    let (name, version) = read_manifest(&out).unwrap();
    assert_eq!(name, "deal-desk", "manifest name comes from spec.name");
    assert_eq!(version, "0.1.0", "scaffold pins version 0.1.0");
    let manifest: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(out.join(".claude-plugin/plugin.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        manifest["description"],
        "Prices and reviews deal desk requests."
    );

    // 5. .gitignore ignores local workstation state.
    let gitignore = std::fs::read_to_string(out.join(".gitignore")).unwrap();
    assert!(gitignore.contains(".agentos/"), "{gitignore}");
}

/// Every skill in a multi-skill spec becomes its own SKILL.md carrying that
/// skill's name, description, allowed-tools (only when present), and body.
#[test]
fn writes_one_skill_md_per_skill_with_frontmatter_and_body() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(valid_spec_json()).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    // Skill one: has allowed_tools -> allowed-tools YAML block with each tool.
    // description and each tool are emitted as quoted YAML scalars (arbitrary
    // agent-authored text must be a quoted scalar or it corrupts/invalidates the
    // frontmatter `plugin_format.validate_bundle` parses with `yaml.safe_load`).
    let deal = std::fs::read_to_string(out.join("skills/deal-desk/SKILL.md")).unwrap();
    assert!(deal.starts_with("---\nname: deal-desk\n"), "{deal}");
    assert!(
        deal.contains("description: \"Invoke when a rep submits a pricing exception request.\""),
        "{deal}"
    );
    assert!(deal.contains("allowed-tools:"), "{deal}");
    assert!(deal.contains("  - \"WebSearch\""), "{deal}");
    assert!(deal.contains("  - \"WebFetch\""), "{deal}");
    // Instructions body lands after the frontmatter.
    assert!(deal.contains("Deal desk skill body."), "{deal}");
    assert!(
        deal.contains("A rep asks for a pricing exception."),
        "{deal}"
    );

    // Skill two: no allowed_tools -> the allowed-tools key is omitted entirely.
    let renewal = std::fs::read_to_string(out.join("skills/renewal-review/SKILL.md")).unwrap();
    assert!(
        renewal.starts_with("---\nname: renewal-review\n"),
        "{renewal}"
    );
    assert!(
        renewal.contains("description: \"Invoke when a renewal needs a discount review.\""),
        "{renewal}"
    );
    assert!(
        !renewal.contains("allowed-tools:"),
        "empty allowed_tools must omit the key entirely\n{renewal}"
    );
    assert!(renewal.contains("Renewal review skill body."), "{renewal}");
}

/// Regression: a description containing a colon-space (`Deal desk: pricing
/// exceptions`) is a bare YAML scalar corruptor -- `yaml.safe_load` raises
/// `mapping values are not allowed here` and rejects the bundle. The rendered
/// frontmatter must quote it so the exact quoted line is present.
#[test]
fn description_with_a_colon_is_rendered_as_a_quoted_scalar() {
    let body = r#"{
      "name": "colon-desc",
      "description": "Bundle desc.",
      "skills": [
        { "name": "solo", "description": "Deal desk: pricing exceptions", "instructions": "Body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(body).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    let solo = std::fs::read_to_string(out.join("skills/solo/SKILL.md")).unwrap();
    assert!(
        solo.contains("description: \"Deal desk: pricing exceptions\""),
        "colon-space description must be a quoted scalar\n{solo}"
    );
}

/// Regression: a description containing ` #` (`Reviews deals #1 priority`) is
/// treated as a comment by `yaml.safe_load` and silently TRUNCATES the value.
/// Quoting keeps the ` #` inside the string.
#[test]
fn description_with_a_hash_is_rendered_as_a_quoted_scalar() {
    let body = r#"{
      "name": "hash-desc",
      "description": "Bundle desc.",
      "skills": [
        { "name": "solo", "description": "Reviews deals #1 priority", "instructions": "Body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(body).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    let solo = std::fs::read_to_string(out.join("skills/solo/SKILL.md")).unwrap();
    assert!(
        solo.contains("description: \"Reviews deals #1 priority\""),
        "` #` description must be quoted so it is not read as a YAML comment\n{solo}"
    );
}

/// A connector in the spec becomes a `.mcp.json` `mcpServers` entry verbatim.
#[test]
fn writes_mcp_json_with_the_spec_connectors() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(valid_spec_json()).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    let mcp: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join(".mcp.json")).unwrap()).unwrap();
    assert!(mcp["mcpServers"].is_object(), "{mcp}");
    assert_eq!(mcp["mcpServers"]["crm"]["command"], "crm-mcp", "{mcp}");
}

/// A spec with no connectors still writes a valid `.mcp.json` with an empty
/// `mcpServers` object (the default), not a missing file.
#[test]
fn writes_empty_mcp_servers_when_no_connectors() {
    let body = r#"{
      "name": "no-conn",
      "description": "No connectors here.",
      "skills": [
        { "name": "solo", "description": "Do the thing.", "instructions": "Solo body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(body).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    let mcp: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(out.join(".mcp.json")).unwrap()).unwrap();
    assert!(mcp["mcpServers"].is_object(), "{mcp}");
    assert_eq!(
        mcp["mcpServers"].as_object().unwrap().len(),
        0,
        "no connectors must yield an empty mcpServers object\n{mcp}"
    );
}

/// The scaffolded `evals/cases.json` is the suite object and is loadable by the
/// SAME loader `agentos skill eval` uses. This is the parity guarantee: the
/// eval suite an agent-authored spec produces must load unchanged on the eval
/// path. Deleting the eval-writing impl breaks `load_suite`.
#[test]
fn scaffolded_evals_load_through_the_skill_eval_loader() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(valid_spec_json()).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();

    let suite = load_suite(&out.join("evals/cases.json")).expect("suite loads");
    assert_eq!(suite.name, "deal-desk", "suite name is the spec name");
    assert_eq!(suite.cases.len(), 1, "one eval in the spec -> one case");
    let case = &suite.cases[0];
    assert_eq!(case.id, "prices-a-deal");
    assert_eq!(case.grader.kind, GraderKind::Contains);
    assert_eq!(case.grader.expected, "all done");
}

// --- validation / error behavior ------------------------------------------

/// A spec `name` that is not kebab-case is rejected, and the message names the
/// offending value so the author can fix it.
#[test]
fn rejects_a_non_kebab_spec_name() {
    let body = valid_spec_json().replace("\"name\": \"deal-desk\"", "\"name\": \"Deal_Desk\"");
    let err = parse(&body)
        .expect_err("bad spec name must error")
        .to_string();
    assert!(
        err.contains("Deal_Desk"),
        "message must name the value: {err}"
    );
}

/// A skill `name` that is not kebab-case is rejected with an actionable message.
#[test]
fn rejects_a_non_kebab_skill_name() {
    let body = valid_spec_json().replace(
        "\"name\": \"renewal-review\"",
        "\"name\": \"Renewal_Review\"",
    );
    let err = parse(&body)
        .expect_err("bad skill name must error")
        .to_string();
    assert!(
        err.contains("Renewal_Review"),
        "message must name the value: {err}"
    );
}

/// Two skills sharing a name is an authoring error (they collide on the same
/// `skills/<name>/SKILL.md` path), rejected before any disk write.
#[test]
fn rejects_duplicate_skill_names() {
    let body = r#"{
      "name": "dupe",
      "description": "Two skills, one name.",
      "skills": [
        { "name": "same", "description": "First.", "instructions": "A body.\n" },
        { "name": "same", "description": "Second.", "instructions": "B body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let err = parse(body)
        .expect_err("duplicate skill names must error")
        .to_string();
    assert!(
        err.contains("same"),
        "message must name the duplicate: {err}"
    );
}

/// A spec must declare at least one skill.
#[test]
fn rejects_an_empty_skills_array() {
    let body = r#"{
      "name": "empty-skills",
      "description": "No skills.",
      "skills": [],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let err = parse(body)
        .expect_err("empty skills must error")
        .to_string();
    assert!(
        err.to_lowercase().contains("skill"),
        "message must mention skills: {err}"
    );
}

/// A spec must declare at least one eval (mirrors `load_suite`'s empty-cases
/// rejection: an empty suite is not runnable).
#[test]
fn rejects_an_empty_evals_array() {
    let body = r#"{
      "name": "empty-evals",
      "description": "No evals.",
      "skills": [
        { "name": "solo", "description": "Do it.", "instructions": "Solo body.\n" }
      ],
      "evals": []
    }"#;
    let err = parse(body).expect_err("empty evals must error").to_string();
    assert!(
        err.to_lowercase().contains("eval"),
        "message must mention evals: {err}"
    );
}

/// A connector server object with neither `command` nor `url` is unusable and
/// must be rejected rather than scaffolded into a broken `.mcp.json`.
#[test]
fn rejects_a_connector_without_command_or_url() {
    let body = r#"{
      "name": "bad-conn",
      "description": "Broken connector.",
      "skills": [
        { "name": "solo", "description": "Do it.", "instructions": "Solo body.\n" }
      ],
      "connectors": {
        "crm": { "args": ["--stdio"] }
      },
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let err = parse(body)
        .expect_err("connector without command/url must error")
        .to_string();
    assert!(
        err.contains("command") || err.contains("url") || err.contains("crm"),
        "message must explain the missing command/url: {err}"
    );
}

/// A connector whose `command` is present but not a STRING (`{"command": 42}`)
/// is rejected: the frozen `McpServer` types `command`/`url` as `str`, so a
/// non-string would pass a mere presence check but make `validate_bundle` reject
/// the emitted `.mcp.json`. The message must name the connector and the field.
#[test]
fn rejects_a_connector_with_a_non_string_command() {
    let body = r#"{
      "name": "typed-conn",
      "description": "Wrong-typed connector.",
      "skills": [
        { "name": "solo", "description": "Do it.", "instructions": "Solo body.\n" }
      ],
      "connectors": {
        "crm": { "command": 42 }
      },
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let err = parse(body)
        .expect_err("non-string command must error")
        .to_string();
    assert!(
        err.contains("crm") && err.contains("command") && err.contains("string"),
        "message must name the connector and the wrong-typed field: {err}"
    );
}

/// An unknown top-level field (a typo like `skils`) is rejected via
/// `deny_unknown_fields`, so a mistyped spec fails loudly instead of silently
/// dropping the intended field. This is the intended strict-parse design.
#[test]
fn rejects_an_unknown_top_level_field() {
    let body = r#"{
      "name": "typo",
      "description": "Typo'd key.",
      "skils": [
        { "name": "solo", "description": "Do it.", "instructions": "Solo body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "contains", "expected": "all done" } }
      ]
    }"#;
    let err = parse(body)
        .expect_err("unknown field must error")
        .to_string();
    assert!(
        err.contains("skils"),
        "message must name the unknown field: {err}"
    );
}

/// An eval carrying an invalid regex grader is rejected at parse/scaffold time,
/// reusing the `load_suite` regex-at-load discipline (a bad pattern fails now,
/// not mid-run).
#[test]
fn rejects_an_invalid_regex_grader_in_a_spec_eval() {
    let body = r#"{
      "name": "bad-regex",
      "description": "Invalid regex grader.",
      "skills": [
        { "name": "solo", "description": "Do it.", "instructions": "Solo body.\n" }
      ],
      "evals": [
        { "id": "e1", "input": "go", "grader": { "kind": "regex", "expected": "(unclosed" } }
      ]
    }"#;
    // Either parse or scaffold must reject it; assert the combined pipeline errors.
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let result = parse(body).and_then(|spec| scaffold_from_spec(&out, &spec).map(|_| ()));
    let err = result
        .expect_err("invalid regex grader must error")
        .to_string();
    assert!(
        err.contains("(unclosed") || err.to_lowercase().contains("regex"),
        "message must name the bad pattern: {err}"
    );
}

// --- collision refusal (mirrors existing scaffold discipline) --------------

/// Scaffolding twice into the same dir refuses the second time rather than
/// truncating the first bundle.
#[test]
fn refuses_to_scaffold_twice_into_the_same_dir() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    let spec = parse(valid_spec_json()).unwrap();
    scaffold_from_spec(&out, &spec).unwrap();
    assert!(
        scaffold_from_spec(&out, &spec).is_err(),
        "second scaffold into the same dir must refuse"
    );
}

/// If a single target file already exists, scaffold refuses, leaves that file
/// untouched, and does not create the manifest. Mirrors the existing
/// `refuses_to_truncate_any_existing_target_even_without_a_manifest` test.
#[test]
fn refuses_to_truncate_an_existing_target_and_writes_no_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");
    std::fs::create_dir_all(&out).unwrap();
    let existing = r#"{"mcpServers":{"important":{"command":"x"}}}"#;
    std::fs::write(out.join(".mcp.json"), existing).unwrap();

    let spec = parse(valid_spec_json()).unwrap();
    let err = scaffold_from_spec(&out, &spec).unwrap_err().to_string();
    assert!(err.contains(".mcp.json"), "{err}");
    assert_eq!(
        std::fs::read_to_string(out.join(".mcp.json")).unwrap(),
        existing,
        "existing file must be untouched"
    );
    assert!(
        !out.join(".claude-plugin/plugin.json").exists(),
        "no manifest may be written when a target collides"
    );
}

// --- CLI surface through the agentos binary --------------------------------

/// `agentos init --from-spec <valid>` is fully non-interactive (no stdin),
/// exits 0, and produces a bundle whose manifest name is the SPEC's name (not
/// any positional argument -- none is passed).
#[test]
fn cli_init_from_spec_scaffolds_non_interactively() {
    let dir = tempfile::tempdir().unwrap();
    let spec_path = write_spec(dir.path(), valid_spec_json());
    let out = dir.path().join("bundle");

    let output = Command::new(bin())
        .arg("init")
        .arg("--from-spec")
        .arg(&spec_path)
        .arg("--dir")
        .arg(&out)
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run agentos init --from-spec");

    assert!(
        output.status.success(),
        "expected success\n{}",
        output_text(&output)
    );
    let (name, _version) = read_manifest(&out).expect("manifest written");
    assert_eq!(
        name, "deal-desk",
        "bundle name is the spec name, not a positional"
    );
}

/// `agentos init --from-spec <invalid>` exits non-zero and names the problem.
#[test]
fn cli_init_from_spec_rejects_an_invalid_spec() {
    let dir = tempfile::tempdir().unwrap();
    // Non-kebab name is a deterministic input error.
    let body = valid_spec_json().replace("\"name\": \"deal-desk\"", "\"name\": \"Deal_Desk\"");
    let spec_path = write_spec(dir.path(), &body);
    let out = dir.path().join("bundle");

    let output = Command::new(bin())
        .arg("init")
        .arg("--from-spec")
        .arg(&spec_path)
        .arg("--dir")
        .arg(&out)
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run agentos init --from-spec");

    assert!(
        !output.status.success(),
        "expected failure for an invalid spec\n{}",
        output_text(&output)
    );
    assert!(
        output_text(&output).contains("Deal_Desk"),
        "message must name the offending value\n{}",
        output_text(&output)
    );
}

/// `agentos init` with NEITHER a positional name NOR `--from-spec` exits
/// non-zero and tells the user to pass a name or `--from-spec`.
#[test]
fn cli_init_without_name_or_spec_errors_with_guidance() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("bundle");

    let output = Command::new(bin())
        .arg("init")
        .arg("--dir")
        .arg(&out)
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run agentos init with no name");

    assert!(
        !output.status.success(),
        "expected failure when neither name nor --from-spec is given\n{}",
        output_text(&output)
    );
    let text = output_text(&output).to_lowercase();
    assert!(
        text.contains("name") && text.contains("from-spec"),
        "message must point at name or --from-spec\n{}",
        output_text(&output)
    );
}
