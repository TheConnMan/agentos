//! Integration: the non-interactive audit + `--json` global flag + semantic
//! exit codes (ADR-0021 decision 1, AC 1, AC 2, AC 3). Every mutating command
//! exposes `--yes`; `--json` parses globally and on the agent-facing read
//! verbs; and error exits carry the right semantic code. The exit-code checks
//! are hermetic (no server, no network topology assumptions beyond a closed
//! local port).

use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn help(args: &[&str]) -> (bool, String) {
    let output = Command::new(bin())
        .args(args)
        .arg("--help")
        .output()
        .expect("run agentos --help");
    let text = String::from_utf8_lossy(&output.stdout).into_owned()
        + &String::from_utf8_lossy(&output.stderr);
    (output.status.success(), text)
}

#[test]
fn mutating_commands_expose_yes_flag() {
    // AC3: every mutating command has a non-interactive `--yes` path so an agent
    // never has to answer a stdin prompt to complete a destructive action.
    let cases: &[&[&str]] = &[
        &["local", "down"],
        &["cluster", "down"],
        &["cluster", "kill"],
        &["cluster", "delete"],
    ];
    for args in cases.iter().copied() {
        let (ok, text) = help(args);
        assert!(ok, "help for {args:?} should succeed:\n{text}");
        assert!(
            text.contains("--yes"),
            "{args:?} --help must list a --yes flag:\n{text}"
        );
    }
}

#[test]
fn json_global_flag_parses_on_skill_status_and_eval() {
    // AC1: `--json` is a global flag; it must parse on the agent-facing read
    // verbs. Fails until `--json` is added to the top-level Cli.
    for args in [["skill", "status", "--json"], ["skill", "eval", "--json"]] {
        let (ok, text) = help(&args);
        assert!(
            ok,
            "`agentos {} --help` must exit 0 (global --json):\n{text}",
            args.join(" ")
        );
    }
}

#[test]
fn json_global_flag_parses_before_subcommand() {
    // The global flag must also parse ahead of the subcommand path.
    let (ok, text) = help(&["--json", "skill", "status"]);
    assert!(
        ok,
        "`agentos --json skill status --help` must exit 0:\n{text}"
    );
}

#[test]
fn skill_status_connection_refused_exits_transient() {
    // Hermetic: port 1 is reserved/closed, so the runner status call fails with
    // a connect error -> transient class -> exit 3 (safe to retry). RED until
    // the transient classification + main wiring lands (today: exit 1).
    let output = Command::new(bin())
        .args(["skill", "status", "--url", "http://127.0.0.1:1"])
        .output()
        .expect("run agentos skill status");
    assert_eq!(
        output.status.code(),
        Some(3),
        "connection refused must map to the transient exit code (3)\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn cluster_kill_without_yes_exits_usage() {
    // Hermetic: the missing-`--yes` guard fires before any network call, so no
    // server is needed. RED until usage classification lands (today: exit 1
    // via a plain anyhow bail).
    let output = Command::new(bin())
        .args(["cluster", "kill", "someagent"])
        .output()
        .expect("run agentos cluster kill");
    assert_eq!(
        output.status.code(),
        Some(2),
        "missing --yes must map to the usage exit code (2)\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}
