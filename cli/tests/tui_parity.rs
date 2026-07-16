//! Parity gate between the TUI recipe catalog and the live CLI grammar.
//!
//! What it does: execs each Command recipe's argv (with placeholder field
//! values) plus `--help` against the built `agentos` binary and asserts it
//! resolves, mirroring the manifest gate in `command_surface.rs`.
//!
//! What it catches: a recipe pointing at a renamed or removed verb, or
//! passing a renamed or removed flag, now fails CI. Clap exits non-zero on
//! an unknown subcommand or flag even when `--help` is present.
//!
//! What it does NOT catch: `--help` short-circuits clap's required-argument
//! validation, so this gate will not detect a verb that gains a new required
//! argument if the recipe is not updated to supply it. Catching that would
//! require parsing the argv in-process through the clap `Command` (without
//! `--help`), which is not possible from an integration test today because
//! the `Cli` grammar is defined in the binary crate (`main.rs`), not the
//! library. Relocating the grammar into the library to enable in-process
//! parsing is a separate, larger change.

use std::process::Command;

use agentos::recipes::command_recipe_argvs;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_agentos")
}

fn run_help(argv: &[String]) -> std::process::Output {
    Command::new(bin())
        .args(argv)
        .arg("--help")
        .output()
        .expect("run agentos --help")
}

fn output_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

/// Every Command recipe in the TUI catalog must resolve to a real verb with
/// real flags in the live CLI grammar. `<argv> --help` exits 0 only when
/// every token in argv is still a valid subcommand/flag path -- clap errors
/// on an unknown verb or flag even with --help present, so success here is
/// equivalent to "this recipe still matches the compiled grammar."
#[test]
fn every_command_recipe_resolves_to_a_real_verb() {
    let recipes = command_recipe_argvs();
    assert!(
        !recipes.is_empty(),
        "expected at least one Command recipe; the RecipeKind::Command filter matched none"
    );

    for (title, argv) in &recipes {
        let output = run_help(argv);
        assert!(
            output.status.success(),
            "recipe {title:?} no longer resolves: argv {argv:?}\n{}",
            output_text(&output)
        );
    }

    eprintln!("tui_parity: exercised {} command recipes", recipes.len());
}

/// The cluster tier must be REACHABLE from the TUI catalog, not just claimed
/// by the Platform tab's tier explainer (#463). The catalog has to expand its
/// tier-bearing recipes into cluster argv, and every one of those argvs has to
/// resolve against the real grammar -- the same `<argv> --help` exec the
/// catalog-wide gate uses, so a cluster verb that does not exist fails here.
#[test]
fn cluster_tier_recipes_are_expanded_and_resolve() {
    let recipes = command_recipe_argvs();
    let cluster: Vec<&(&str, Vec<String>)> = recipes
        .iter()
        .filter(|(_, argv)| argv.first().map(String::as_str) == Some("cluster"))
        .collect();

    assert!(
        !cluster.is_empty(),
        "no cluster argv in the TUI catalog: the cluster tier is unreachable"
    );

    // Governance, not just the pre-existing cluster status/message recipes:
    // a tier-bearing platform recipe must actually reach the cluster tier.
    assert!(
        cluster
            .iter()
            .any(|(_, argv)| argv.get(1).map(String::as_str) == Some("versions")),
        "no `cluster versions` argv: the platform governance recipes are still local-only"
    );

    for (title, argv) in &cluster {
        let output = run_help(argv);
        assert!(
            output.status.success(),
            "cluster recipe {title:?} does not resolve: argv {argv:?}\n{}",
            output_text(&output)
        );
    }

    eprintln!("tui_parity: exercised {} cluster recipes", cluster.len());
}

/// Negative control: proves the gate mechanism actually rejects drift rather
/// than always passing regardless of argv.
#[test]
fn a_bogus_verb_fails_the_gate() {
    let argv = vec!["definitely-not-a-real-verb".to_string()];
    let output = run_help(&argv);
    assert!(
        !output.status.success(),
        "expected failure for a bogus verb\n{}",
        output_text(&output)
    );
}
