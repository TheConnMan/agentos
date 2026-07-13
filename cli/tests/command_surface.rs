use std::process::Command;

use agentos::retired_hint;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn run_help(args: &[&str]) -> std::process::Output {
    Command::new(bin())
        .args(args)
        .arg("--help")
        .output()
        .expect("run agentos --help")
}

fn output_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

fn help_lists_subcommand(text: &str, name: &str) -> bool {
    text.lines().any(|line| {
        let line = line.trim_start();
        line == name
            || line.starts_with(&format!("{name} "))
            || line.starts_with(&format!("{name}\t"))
    })
}

#[test]
fn process_help_routes_positive_forms() {
    let cases: &[&[&str]] = &[
        &["skill", "up"],
        &["skill", "down"],
        &["skill", "status"],
        &["skill", "message"],
        &["skill", "eval"],
        &["skill", "check"],
        &["local", "up"],
        &["local", "down"],
        &["local", "status"],
        &["local", "message"],
        &["local", "eval"],
        &["local", "deploy"],
        &["cluster", "up"],
        &["cluster", "down"],
        &["cluster", "status"],
        &["cluster", "message"],
        &["cluster", "eval"],
        &["cluster", "deploy"],
        &["init"],
    ];

    for args in cases.iter().copied() {
        let output = run_help(args);
        assert!(
            output.status.success(),
            "expected success for {:?}\n{}",
            args,
            output_text(&output)
        );
    }
}

#[test]
fn process_help_rejects_retired_top_level_tokens() {
    let cases = [
        "start",
        "stop",
        "send",
        "eval",
        "runner-status",
        "chat",
        "steer",
        "interrupt",
        "up",
        "down",
        "status",
        "message",
        "deploy",
    ];

    for token in cases {
        let output = run_help(&[token]);
        assert!(
            !output.status.success(),
            "expected failure for {token}\n{}",
            output_text(&output)
        );
    }
}

#[test]
fn process_help_rejects_retired_local_flag_on_a_leaf_command() {
    let output = run_help(&["skill", "message", "hello", "--local"]);
    assert!(
        !output.status.success(),
        "expected failure for retired --local on a leaf command\n{}",
        output_text(&output)
    );
}

#[test]
fn process_help_top_level_lists_new_surface_and_hides_retired_verbs() {
    let output = run_help(&[]);
    assert!(
        output.status.success(),
        "expected success for top level help\n{}",
        output_text(&output)
    );
    let text = output_text(&output);

    for needle in ["skill", "local", "cluster", "init"] {
        assert!(
            help_lists_subcommand(&text, needle),
            "missing {needle}\n{text}"
        );
    }

    for needle in [
        "start",
        "stop",
        "send",
        "eval",
        "runner-status",
        "chat",
        "steer",
        "interrupt",
        "up",
        "down",
        "status",
        "message",
        "deploy",
    ] {
        assert!(
            !help_lists_subcommand(&text, needle),
            "unexpected retired verb {needle}\n{text}"
        );
    }
}

/// The checked-in manifest must equal what `agentos schema` emits from the live
/// clap grammar. This is the generated-artifact + CI drift gate (mirroring the
/// schema-export discipline for `packages/aci-protocol` / `packages/plugin-format`):
/// any grammar change (new command, flag, default, env var, help text) must be
/// accompanied by a regenerated `cli/command-manifest.json` in the same PR.
#[test]
fn command_manifest_matches_committed_artifact() {
    let output = Command::new(bin())
        .arg("schema")
        .output()
        .expect("run agentos schema");
    assert!(
        output.status.success(),
        "agentos schema failed\n{}",
        output_text(&output)
    );
    let generated = String::from_utf8(output.stdout).expect("manifest is utf-8");

    let manifest_path = concat!(env!("CARGO_MANIFEST_DIR"), "/command-manifest.json");
    let committed =
        std::fs::read_to_string(manifest_path).expect("cli/command-manifest.json is committed");

    assert_eq!(
        generated, committed,
        "cli/command-manifest.json is stale; regenerate with \
         `cargo run -- schema > cli/command-manifest.json`"
    );
}

/// `dump-commands` is the documented alias for the hidden `schema` verb.
#[test]
fn dump_commands_alias_emits_same_manifest() {
    let schema = Command::new(bin())
        .arg("schema")
        .output()
        .expect("run agentos schema");
    let alias = Command::new(bin())
        .arg("dump-commands")
        .output()
        .expect("run agentos dump-commands");
    assert!(schema.status.success() && alias.status.success());
    assert_eq!(schema.stdout, alias.stdout);
}

fn to_argv(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_string()).collect()
}

fn assert_hint_contains(argv: &[&str], needle: &str) {
    let argv = to_argv(argv);
    let hint = retired_hint(&argv).unwrap_or_else(|| panic!("expected hint for {:?}", argv));
    assert!(
        hint.contains(needle),
        "expected {needle:?} in hint {hint:?} for {:?}",
        argv
    );
}

fn assert_hint_contains_any(argv: &[&str], needles: &[&str]) {
    let argv = to_argv(argv);
    let hint = retired_hint(&argv).unwrap_or_else(|| panic!("expected hint for {:?}", argv));
    assert!(
        needles.iter().any(|needle| hint.contains(needle)),
        "expected one of {needles:?} in hint {hint:?} for {:?}",
        argv
    );
}

fn assert_hint_none(argv: &[&str]) {
    let argv = to_argv(argv);
    assert_eq!(retired_hint(&argv), None, "unexpected hint for {:?}", argv);
}

#[test]
fn retired_hint_maps_retired_tokens_and_ignores_global_flags() {
    let cases: &[(&[&str], &str)] = &[
        (&["start"], "skill up"),
        (&["stop"], "skill down"),
        (&["send"], "skill message"),
        (&["runner-status"], "skill status"),
        (&["eval"], "skill eval"),
        (&["chat"], "local message"),
        (&["up"], "cluster up"),
        (&["down"], "cluster down"),
        (&["status"], "cluster status"),
        (&["message"], "cluster message"),
        (&["deploy"], "cluster deploy"),
        (&["steer"], "removed"),
        (&["interrupt"], "removed"),
        (&["--local", "start"], "local"),
        (&["start", "--local"], "local"),
        (&["skill", "up", "--local"], "local"),
        (&["--debug", "start"], "skill up"),
        (&["-q", "stop"], "skill down"),
        (&["--quiet", "send"], "skill message"),
        (&["--color", "always", "status"], "cluster status"),
        (&["--color=always", "deploy"], "cluster deploy"),
    ];

    for (argv, needle) in cases.iter().copied() {
        assert_hint_contains(argv, needle);
    }

    assert_hint_contains_any(&["interrupt"], &["Ctrl-C", "skill message"]);
}

#[test]
fn retired_hint_returns_none_for_valid_starts_help_and_message_bodies() {
    let cases: &[&[&str]] = &[
        &["skill", "up"],
        &["skill", "message", "hello"],
        &["skill", "message", "please deploy the thing"],
        &["local", "up"],
        &["local", "message", "please deploy the thing"],
        &["local", "message", "please", "deploy", "the", "thing"],
        &["local", "deploy"],
        &["cluster", "up"],
        &["cluster", "message", "status of the world"],
        &["cluster", "message", "status", "of", "the", "world"],
        &["cluster", "deploy"],
        &["init", "my-plugin"],
        &["--help"],
        &["--debug"],
        &["--debug", "skill", "up"],
        &["--color", "always"],
        &["--color=always"],
        &["--color", "always", "cluster", "status"],
        &["--color", "always", "skill", "up"],
        &["-h"],
        &["-V"],
        &["--version"],
        &[],
    ];

    for argv in cases.iter().copied() {
        assert_hint_none(argv);
    }
}
