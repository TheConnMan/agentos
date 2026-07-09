//! `agentos local <up|down|status>`: wrap the repo's local dev stack
//! (`compose.dev.yaml`: Postgres + Valkey + Langfuse + ClickHouse + MinIO +
//! OTel) the same way `ops.rs` wraps Helm -- a deliberately thin CLI over
//! `docker compose`, which stays the source of truth. Each verb builds its
//! command line as a pure function returning an [`OpsCommand`]; the executor
//! (or the `--dry-run` printer) consumes it, so argv construction stays
//! unit-testable with no Docker daemon.

use anyhow::{bail, Context, Result};

use crate::commands::OLLAMA_PORT;
use crate::docker;
use crate::ops::{plain, require_on_path, run_capture, run_step, OpsCommand};

/// Dev-channel local-candidate filename probed by the artifact resolver.
pub const DEFAULT_COMPOSE_FILE: &str = "compose.dev.yaml";

/// The service endpoints the dev stack exposes, as committed in
/// `compose.dev.yaml`'s port mappings. Printed after `local up` so the operator
/// has the URLs in hand. Hardcoded to match the compose file (see the
/// `endpoints_match_compose_file` test, which asserts the file still maps them).
///
/// The `core` flag marks endpoints backed by a service in the `core` profile
/// (started under `--minimal`); the rest are `full`-only and are hidden under
/// `--minimal` so `up` never advertises a URL for a service it did not start.
const ENDPOINTS: &[(&str, &str, bool)] = &[
    ("AgentOS API", "http://localhost:28000", true),
    ("AgentOS Console", "http://localhost:28080/?api=1", false),
    ("Langfuse UI", "http://localhost:23000", false),
    ("Postgres", "localhost:25432", true),
    ("Valkey", "localhost:26379", true),
    ("ClickHouse HTTP", "localhost:28123", false),
    ("MinIO S3", "localhost:29000", true),
    ("MinIO console", "localhost:29001", true),
    ("OTel gRPC", "localhost:24317", false),
    ("OTel HTTP", "localhost:24318", false),
];

/// Flags shared by every `local` verb.
pub struct LocalOpts {
    pub file: String,
    pub dry_run: bool,
    pub minimal: bool,
    pub local_model: Option<String>,
    pub slack: bool,
}

pub struct LocalDownOpts {
    pub common: LocalOpts,
    /// Add `-v` to destroy volumes (throwaway).
    pub wipe: bool,
    /// Skip the interactive confirmation that `--wipe` otherwise requires.
    pub yes: bool,
}

// ---------------------------------------------------------------------------
// Command builders (pure; unit-tested below)
// ---------------------------------------------------------------------------

/// `docker compose -f <file> <tail...>`.
fn compose(file: &str, tail: &[&str]) -> OpsCommand {
    let mut args = vec![plain("compose"), plain("-f"), plain(file)];
    for t in tail {
        args.push(plain(*t));
    }
    OpsCommand::new("docker", args)
}

/// `docker compose --profile <core|full> [--profile local-model] [--profile slack] -f <file> up -d --wait`.
pub fn up_command(o: &LocalOpts) -> OpsCommand {
    let profile = if o.minimal { "core" } else { "full" };
    let mut args = vec![plain("compose"), plain("--profile"), plain(profile)];
    if o.local_model.is_some() {
        args.push(plain("--profile"));
        args.push(plain("local-model"));
    }
    if o.slack {
        args.push(plain("--profile"));
        args.push(plain("slack"));
    }
    args.extend([
        plain("-f"),
        plain(&o.file),
        plain("up"),
        plain("-d"),
        plain("--wait"),
    ]);
    let mut cmd = OpsCommand::new("docker", args);
    if let Some(model) = &o.local_model {
        cmd = cmd.with_env(vec![
            ("AGENTOS_FAKE_MODEL".into(), "0".into()),
            (
                "AGENTOS_MODEL_BASE_URL".into(),
                format!("http://ollama:{OLLAMA_PORT}"),
            ),
            ("AGENTOS_MODEL".into(), model.clone()),
            ("AGENTOS_DOCKER_NETWORK".into(), "agentos_default".into()),
            // Pin the compose project name so the default network is always
            // `agentos_default`, regardless of the working-directory basename
            // (which is what compose otherwise derives the project name from).
            ("COMPOSE_PROJECT_NAME".into(), "agentos".into()),
        ]);
    }
    cmd
}

/// `docker compose -f <file> down` (keep volumes), or `... down -v` with
/// `--wipe` (destroy volumes).
pub fn down_command(o: &LocalDownOpts) -> OpsCommand {
    if o.wipe {
        compose(&o.common.file, &["down", "-v"])
    } else {
        compose(&o.common.file, &["down"])
    }
}

/// `docker compose -f <file> ps`.
pub fn status_command(o: &LocalOpts) -> OpsCommand {
    compose(&o.file, &["ps"])
}

// ---------------------------------------------------------------------------
// Verb handlers
// ---------------------------------------------------------------------------

pub async fn up(o: LocalOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = up_command(&o);
    if o.dry_run {
        ui.payload_plain(&cmd.display());
        return Ok(());
    }
    require_on_path("docker")?;
    let cl = ui.checklist();
    run_step(&cl, "starting dev stack", "up", &cmd).await?;
    for (label, url, is_core) in ENDPOINTS {
        // Under `--minimal` only the `core` services started, so advertise only
        // their endpoints; the `full`-only URLs would 404.
        if !o.minimal || *is_core {
            ui.kv(label, &ui.url(url));
        }
    }
    if o.slack {
        ui.note("Slack dispatcher started (Socket Mode; no host port).");
    }
    ui.note("Drive the local product loop (no Slack, no Kubernetes):");
    ui.note(
        "  agentos local deploy --plugin-dir <dir> --slack-channel <C...> --api-url http://localhost:28000",
    );
    ui.note("  agentos local message \"<your question>\"");
    Ok(())
}

pub async fn status(o: LocalOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = status_command(&o);
    if o.dry_run {
        ui.payload_plain(&cmd.display());
        return Ok(());
    }
    require_on_path("docker")?;
    // `docker compose ps` output is itself the payload table.
    let (ok, out, err) = run_capture(&cmd).await?;
    if !ok {
        for line in err.lines() {
            ui.plumbing(line);
        }
        let reason = err
            .lines()
            .rev()
            .map(str::trim)
            .find(|l| !l.is_empty())
            .unwrap_or("command failed");
        ui.failure(&format!("`docker compose ps` failed: {reason}"));
        bail!("`docker compose ps` exited nonzero");
    }
    for line in out.lines() {
        ui.payload_plain(line);
    }
    Ok(())
}

pub async fn down(o: LocalDownOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = down_command(&o);
    if o.common.dry_run {
        ui.payload_plain(&cmd.display());
        ui.payload_plain(&format!(
            "docker rm -f $(docker ps -a --filter label={} -q)",
            docker::SANDBOX_LABEL
        ));
        return Ok(());
    }
    if o.wipe {
        ui.warn(&format!(
            "this destroys all volumes for the '{}' dev stack (Postgres, ClickHouse, MinIO, Valkey data)",
            o.common.file
        ));
        if !o.yes && !confirm_wipe(&o.common.file)? {
            ui.note("aborted");
            return Ok(());
        }
    }
    require_on_path("docker")?;
    let cl = ui.checklist();
    let label = if o.wipe {
        "stopping stack and wiping volumes"
    } else {
        "stopping stack"
    };
    run_step(&cl, label, "stopped", &cmd).await?;
    let count = docker::reap_labeled(docker::SANDBOX_LABEL).await?;
    if count > 0 {
        ui.note(&format!("removed {count} runner container(s)"));
    }
    if o.wipe {
        ui.payload("dev stack stopped; volumes wiped");
    } else {
        ui.payload("dev stack stopped");
        ui.note("volumes kept (fast restart with `agentos local up`)");
    }
    Ok(())
}

/// Read a y/N confirmation from stderr/stdin before `--wipe` destroys volumes.
fn confirm_wipe(file: &str) -> Result<bool> {
    use std::io::Write;
    eprint!(
        "This destroys all volumes for the '{file}' dev stack (Postgres, ClickHouse, MinIO, Valkey data). Continue? [y/N] "
    );
    std::io::stderr().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .context("reading confirmation from stdin")?;
    Ok(matches!(line.trim(), "y" | "Y" | "yes" | "Yes"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(file: &str) -> LocalOpts {
        LocalOpts {
            file: file.into(),
            dry_run: false,
            minimal: false,
            local_model: None,
            slack: false,
        }
    }

    fn opts_with_local_model(file: &str, model: &str) -> LocalOpts {
        LocalOpts {
            file: file.into(),
            dry_run: false,
            minimal: false,
            local_model: Some(model.into()),
            slack: false,
        }
    }

    fn read_compose(name: &str) -> String {
        std::fs::read_to_string(format!("{}/../{}", env!("CARGO_MANIFEST_DIR"), name))
            .unwrap_or_else(|e| panic!("read {name}: {e}"))
    }

    #[test]
    fn up_uses_detached_wait() {
        let cmd = up_command(&opts(DEFAULT_COMPOSE_FILE));
        assert_eq!(
            cmd.display(),
            "docker compose --profile full -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn up_local_model_uses_profile_and_env() {
        let cmd = up_command(&opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b"));
        let display = cmd.display();
        assert!(display.contains("--profile full"), "{display}");
        assert!(display.contains("--profile local-model"), "{display}");
        assert!(display.contains("up -d --wait"), "{display}");
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_FAKE_MODEL"), String::from("0"))));
        assert!(cmd.env.contains(&(
            String::from("AGENTOS_MODEL_BASE_URL"),
            String::from("http://ollama:11434"),
        )));
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
        assert!(cmd.env.contains(&(
            String::from("AGENTOS_DOCKER_NETWORK"),
            String::from("agentos_default"),
        )));
        assert!(cmd.env.contains(&(
            String::from("COMPOSE_PROJECT_NAME"),
            String::from("agentos"),
        )));
    }

    #[test]
    fn up_slack_appends_slack_profile() {
        let mut opts = opts(DEFAULT_COMPOSE_FILE);
        opts.slack = true;
        let cmd = up_command(&opts);
        assert_eq!(
            cmd.display(),
            "docker compose --profile full --profile slack -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn up_minimal_slack_uses_core_and_slack() {
        let mut opts = opts(DEFAULT_COMPOSE_FILE);
        opts.minimal = true;
        opts.slack = true;
        let display = up_command(&opts).display();
        assert!(display.contains("--profile core"), "{display}");
        assert!(display.contains("--profile slack"), "{display}");
        assert!(!display.contains("--profile full"), "{display}");
    }

    #[test]
    fn up_local_model_and_slack_keep_profile_order() {
        let mut opts = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        opts.slack = true;
        let display = up_command(&opts).display();
        assert!(
            display.contains("--profile full --profile local-model --profile slack"),
            "{display}"
        );
    }

    #[test]
    fn up_minimal_uses_core_profile() {
        let mut o = opts(DEFAULT_COMPOSE_FILE);
        o.minimal = true;
        let cmd = up_command(&o);
        assert_eq!(
            cmd.display(),
            "docker compose --profile core -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn minimal_and_local_model_combine() {
        let mut o = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        o.minimal = true;
        let cmd = up_command(&o);
        let display = cmd.display();
        assert!(display.contains("--profile core"), "{display}");
        assert!(display.contains("--profile local-model"), "{display}");
        assert!(!display.contains("--profile full"), "{display}");
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
        assert!(cmd.env.contains(&(
            String::from("COMPOSE_PROJECT_NAME"),
            String::from("agentos"),
        )));
    }

    #[test]
    fn status_runs_ps() {
        let cmd = status_command(&opts(DEFAULT_COMPOSE_FILE));
        assert_eq!(cmd.display(), "docker compose -f compose.dev.yaml ps");
    }

    #[test]
    fn down_keeps_volumes_by_default() {
        let cmd = down_command(&LocalDownOpts {
            common: opts(DEFAULT_COMPOSE_FILE),
            wipe: false,
            yes: false,
        });
        assert_eq!(cmd.display(), "docker compose -f compose.dev.yaml down");
    }

    #[test]
    fn down_wipe_adds_volume_flag() {
        let cmd = down_command(&LocalDownOpts {
            common: opts(DEFAULT_COMPOSE_FILE),
            wipe: true,
            yes: false,
        });
        assert_eq!(cmd.display(), "docker compose -f compose.dev.yaml down -v");
    }

    #[test]
    fn custom_file_flows_through_every_verb() {
        let f = "compose.other.yaml";
        assert!(up_command(&opts(f))
            .display()
            .contains("-f compose.other.yaml"));
        assert!(status_command(&opts(f))
            .display()
            .contains("-f compose.other.yaml"));
        let down = down_command(&LocalDownOpts {
            common: opts(f),
            wipe: true,
            yes: true,
        });
        assert_eq!(
            down.display(),
            "docker compose -f compose.other.yaml down -v"
        );
    }

    /// The endpoint constants are hardcoded; this asserts they still match the
    /// port mappings in the committed compose file (the "verify against the
    /// file" the task asks for, kept mechanical).
    #[test]
    fn endpoints_match_compose_file() {
        let compose = read_compose("compose.dev.yaml");
        // Each printed host port must appear as a `"<host>:<container>"` mapping.
        for (label, host_port) in [
            ("AgentOS API", "28000"),
            ("AgentOS Console", "28080"),
            ("Langfuse UI", "23000"),
            ("Postgres", "25432"),
            ("Valkey", "26379"),
            ("ClickHouse HTTP", "28123"),
            ("MinIO S3", "29000"),
            ("MinIO console", "29001"),
            ("OTel gRPC", "24317"),
            ("OTel HTTP", "24318"),
        ] {
            assert!(
                compose.contains(&format!("\"{host_port}:")),
                "compose.dev.yaml no longer maps host port {host_port} for {label}"
            );
            assert!(
                ENDPOINTS.iter().any(|(_, url, _)| url.contains(host_port)),
                "ENDPOINTS missing {host_port} for {label}"
            );
        }
        // The console must be advertised in wired mode (?api=1); the published UI
        // image is fixture-by-default and only talks to the API when the URL
        // carries this param.
        let console = ENDPOINTS
            .iter()
            .find(|(label, _, _)| *label == "AgentOS Console")
            .expect("AgentOS Console endpoint present");
        assert!(
            console.1.contains("api=1"),
            "AgentOS Console endpoint must be the wired ?api=1 URL, got {}",
            console.1
        );
    }

    /// The 7 services that must carry `profiles: *core_profiles`.
    const CORE_SERVICES: &[&str] = &[
        "postgres",
        "valkey",
        "minio",
        "minio-init",
        "agentos-migrate",
        "agentos-api",
        "agentos-worker",
    ];

    /// The 5 services that must carry `profiles: *full_profiles`.
    const FULL_SERVICES: &[&str] = &[
        "clickhouse",
        "langfuse-worker",
        "langfuse-web",
        "otel-collector",
        "agentos-ui",
    ];

    /// Return the YAML block for `service`: everything from its `  <service>:`
    /// header up to the next top-level (2-space-indented) service header. Used
    /// to assert a profile anchor lives inside the *right* service block, so a
    /// per-service profile swap fails the test rather than passing on counts.
    fn service_block<'a>(compose: &'a str, service: &str) -> &'a str {
        let header = format!("\n  {service}:\n");
        let start = compose
            .find(&header)
            .unwrap_or_else(|| panic!("service {service} not found"));
        let after = start + header.len();
        let rest = &compose[after..];
        // The next service header is the next "\n  " whose following char is not
        // a space (deeper-indented keys start with "\n    ").
        let end = rest
            .match_indices("\n  ")
            .find(|(i, _)| rest[i + 3..].starts_with(|c: char| c != ' '))
            .map(|(i, _)| i)
            .unwrap_or(rest.len());
        &rest[..end]
    }

    /// Assert the shared core(7)/full(5) profile binding in a compose file:
    /// the anchors are declared, the counts hold, AND each service block carries
    /// the anchor it should (so swapping a service's profile fails the test).
    fn assert_core_full_bindings(compose: &str, file: &str) {
        assert!(
            compose.contains("x-core-profiles: &core_profiles [core, full]"),
            "{file} missing core anchor"
        );
        assert!(
            compose.contains("x-full-profiles: &full_profiles [full]"),
            "{file} missing full anchor"
        );
        assert_eq!(
            compose.matches("profiles: *core_profiles").count(),
            7,
            "{file} core-profile count"
        );
        assert_eq!(
            compose.matches("profiles: *full_profiles").count(),
            5,
            "{file} full-profile count"
        );
        for service in CORE_SERVICES {
            let block = service_block(compose, service);
            assert!(
                block.contains("profiles: *core_profiles"),
                "{file}: {service} block must bind *core_profiles"
            );
            assert!(
                !block.contains("profiles: *full_profiles"),
                "{file}: {service} block must not bind *full_profiles"
            );
        }
        for service in FULL_SERVICES {
            let block = service_block(compose, service);
            assert!(
                block.contains("profiles: *full_profiles"),
                "{file}: {service} block must bind *full_profiles"
            );
            assert!(
                !block.contains("profiles: *core_profiles"),
                "{file}: {service} block must not bind *core_profiles"
            );
        }
    }

    /// Lock which endpoints are advertised under `--minimal`: exactly the five
    /// backed by a `core`-profile service. A core/full mislabel here would print
    /// a dead URL (or hide a live one) under `--minimal`.
    #[test]
    fn minimal_advertises_only_core_endpoints() {
        let core: Vec<&str> = ENDPOINTS
            .iter()
            .filter(|(_, _, is_core)| *is_core)
            .map(|(label, _, _)| *label)
            .collect();
        assert_eq!(
            core,
            vec![
                "AgentOS API",
                "Postgres",
                "Valkey",
                "MinIO S3",
                "MinIO console",
            ]
        );
    }

    #[test]
    fn compose_file_declares_core_and_full_profiles() {
        let compose = read_compose("compose.dev.yaml");
        assert_core_full_bindings(&compose, "compose.dev.yaml");
    }

    #[test]
    fn compose_file_makes_worker_slack_stub_overridable() {
        let compose = read_compose("compose.dev.yaml");
        assert!(compose.contains(
            "      - SLACK_API_BASE_URL=${SLACK_API_BASE_URL-http://localhost:8155/api/}"
        ));
        assert!(compose.contains("      - SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-xoxb-dev}"));
    }

    #[test]
    fn compose_file_declares_slack_dispatcher_profile() {
        let compose = read_compose("compose.dev.yaml");
        let dispatcher = compose
            .split("  agentos-dispatcher:")
            .nth(1)
            .expect("agentos-dispatcher service present");
        assert!(dispatcher.contains("    profiles: [slack]"));
        assert!(!dispatcher.contains("profiles: *core_profiles"));
        assert!(!dispatcher.contains("profiles: *full_profiles"));
        assert!(dispatcher.contains("      VALKEY_HOST: valkey"));
        assert!(dispatcher.contains("      SLACK_APP_TOKEN: ${SLACK_APP_TOKEN:-}"));
    }
}
