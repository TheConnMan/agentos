//! A machine-readable snapshot of the CLI grammar.
//!
//! [`manifest`] walks a built `clap::Command` tree and emits structured JSON:
//! every target/verb, its args and flags, their env vars, defaults, and help
//! text. The `agentos schema` (hidden) subcommand prints [`manifest_json`] to
//! stdout; the checked-in `cli/command-manifest.json` is that output, and
//! `cli/tests/command_surface.rs` regenerates it and fails on drift -- the same
//! generated-artifact-plus-CI-gate discipline the repo applies to
//! `packages/aci-protocol` and `packages/plugin-format`.
//!
//! It reads the clap grammar reflectively (never a hand-maintained copy), so any
//! new command, flag, default, or help string shows up in the manifest the
//! moment the grammar changes -- and the drift gate then requires the committed
//! file to be regenerated in the same PR.

use clap::builder::StyledStr;
use serde_json::{json, Map, Value};

/// Build the full command manifest as a JSON value rooted at the top-level
/// command.
pub fn manifest(command: &clap::Command) -> Value {
    command_to_value(command)
}

/// Pretty-printed manifest JSON with a trailing newline (so the checked-in file
/// is POSIX-clean and matches `println!`-style regeneration).
pub fn manifest_json(command: &clap::Command) -> String {
    let mut out = serde_json::to_string_pretty(&manifest(command))
        .expect("manifest is plain JSON and always serializes");
    out.push('\n');
    out
}

fn styled_to_string(value: Option<&StyledStr>) -> Option<String> {
    value.map(std::string::ToString::to_string)
}

fn command_to_value(command: &clap::Command) -> Value {
    let mut obj = Map::new();
    obj.insert("name".into(), json!(command.get_name()));
    if let Some(about) = styled_to_string(command.get_about()) {
        obj.insert("about".into(), json!(about));
    }
    if let Some(long_about) = styled_to_string(command.get_long_about()) {
        obj.insert("long_about".into(), json!(long_about));
    }
    obj.insert("hidden".into(), json!(command.is_hide_set()));

    let aliases: Vec<String> = command.get_visible_aliases().map(str::to_string).collect();
    if !aliases.is_empty() {
        obj.insert("aliases".into(), json!(aliases));
    }

    // Declaration order is stable across builds, so the manifest is
    // deterministic and diffs cleanly.
    let args: Vec<Value> = command.get_arguments().map(arg_to_value).collect();
    if !args.is_empty() {
        obj.insert("args".into(), Value::Array(args));
    }

    let subcommands: Vec<Value> = command.get_subcommands().map(command_to_value).collect();
    if !subcommands.is_empty() {
        obj.insert("subcommands".into(), Value::Array(subcommands));
    }

    Value::Object(obj)
}

fn arg_to_value(arg: &clap::Arg) -> Value {
    let mut obj = Map::new();
    obj.insert("id".into(), json!(arg.get_id().as_str()));

    if let Some(help) = styled_to_string(arg.get_help()) {
        obj.insert("help".into(), json!(help));
    }
    if let Some(long) = arg.get_long() {
        obj.insert("long".into(), json!(long));
    }
    if let Some(short) = arg.get_short() {
        obj.insert("short".into(), json!(short.to_string()));
    }
    if let Some(env) = arg.get_env() {
        obj.insert("env".into(), json!(env.to_string_lossy()));
    }

    obj.insert("positional".into(), json!(arg.is_positional()));
    obj.insert("required".into(), json!(arg.is_required_set()));
    obj.insert("global".into(), json!(arg.is_global_set()));

    // A credential's default is deliberately NOT emitted (#630). The console
    // bundles this manifest into `dist/`, so any default here is a static asset
    // served to every browser -- and `--api-key`'s default IS the API's own
    // `Settings.api_key` default, i.e. the live platform key on a dev install.
    // `hide_env_values` is the marker: it is already how this CLI declares "this
    // arg carries a credential" (cli/CLAUDE.md), so keying off it means a new
    // credential arg is covered the moment it follows the existing convention,
    // rather than needing to be remembered here. The arg, its `--help`, and its
    // runtime default are all unchanged; only the machine-readable snapshot
    // stops carrying the value.
    if !arg.is_hide_env_values_set() {
        let defaults: Vec<String> = arg
            .get_default_values()
            .iter()
            .map(|v| v.to_string_lossy().into_owned())
            .collect();
        if !defaults.is_empty() {
            obj.insert("default_values".into(), json!(defaults));
        }
    }

    let possible: Vec<String> = arg
        .get_possible_values()
        .iter()
        .map(|pv| pv.get_name().to_string())
        .collect();
    if !possible.is_empty() {
        obj.insert("possible_values".into(), json!(possible));
    }

    if let Some(range) = arg.get_num_args() {
        obj.insert(
            "num_args".into(),
            json!({
                "min": range.min_values(),
                "max": range.max_values(),
            }),
        );
    }

    Value::Object(obj)
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{Arg, ArgAction, Command};

    fn sample() -> Command {
        Command::new("demo")
            .about("Demo CLI")
            .arg(
                Arg::new("verbose")
                    .long("verbose")
                    .short('v')
                    .action(ArgAction::SetTrue)
                    .global(true)
                    .help("Chatter"),
            )
            .subcommand(
                Command::new("run").about("Run it").arg(
                    Arg::new("port")
                        .long("port")
                        .env("DEMO_PORT")
                        .default_value("8080")
                        .help("Port"),
                ),
            )
            .subcommand(Command::new("hidden-one").hide(true))
            .subcommand(
                Command::new("auth").about("Auth it").arg(
                    Arg::new("api-key")
                        .long("api-key")
                        .env("DEMO_API_KEY")
                        .hide_env_values(true)
                        .default_value("demo-dev-key")
                        .help("Key"),
                ),
            )
    }

    /// A credential arg's default must never reach the manifest (#630): the
    /// console bundles this file, so a default here is a credential served as a
    /// static asset. The arg itself must still be described in full, or the
    /// console's `cliCommand()` hints lose a real flag.
    #[test]
    fn manifest_omits_a_credential_args_default_but_keeps_the_arg() {
        let value = manifest(&sample());
        let subs = value["subcommands"].as_array().expect("subcommands");
        let auth = subs.iter().find(|c| c["name"] == "auth").expect("auth");
        let key = auth["args"]
            .as_array()
            .expect("auth args")
            .iter()
            .find(|a| a["id"] == "api-key")
            .expect("api-key arg");

        assert!(
            key.get("default_values").is_none(),
            "a credential default must not be emitted into the manifest: {key:?}"
        );
        assert!(
            !manifest_json(&sample()).contains("demo-dev-key"),
            "the credential value must not appear anywhere in the manifest"
        );
        // The arg is described, just without its value.
        assert_eq!(key["long"], "api-key");
        assert_eq!(key["env"], "DEMO_API_KEY");
        assert_eq!(key["help"], "Key");
    }

    #[test]
    fn manifest_captures_names_help_and_nesting() {
        let value = manifest(&sample());
        assert_eq!(value["name"], "demo");
        assert_eq!(value["about"], "Demo CLI");

        let subs = value["subcommands"].as_array().expect("subcommands");
        let run = subs.iter().find(|c| c["name"] == "run").expect("run");
        let args = run["args"].as_array().expect("run args");
        let port = args.iter().find(|a| a["id"] == "port").expect("port arg");
        assert_eq!(port["long"], "port");
        assert_eq!(port["env"], "DEMO_PORT");
        assert_eq!(port["default_values"][0], "8080");
        assert_eq!(port["help"], "Port");
    }

    #[test]
    fn manifest_records_hidden_and_global_flags() {
        let value = manifest(&sample());
        let subs = value["subcommands"].as_array().unwrap();
        let hidden = subs
            .iter()
            .find(|c| c["name"] == "hidden-one")
            .expect("hidden-one");
        assert_eq!(hidden["hidden"], true);

        let verbose = value["args"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["id"] == "verbose")
            .expect("verbose");
        assert_eq!(verbose["global"], true);
        assert_eq!(verbose["short"], "v");
    }

    #[test]
    fn manifest_json_is_pretty_and_newline_terminated() {
        let text = manifest_json(&sample());
        assert!(text.ends_with("}\n"));
        assert!(text.contains("\n  \"name\""));
    }
}
