//! `agentos local <up|down|status>`: wrap the repo's local dev stack
//! (`compose.dev.yaml`: Postgres + Valkey + Langfuse + ClickHouse + MinIO +
//! OTel) the same way `ops.rs` wraps Helm -- a deliberately thin CLI over
//! `docker compose`, which stays the source of truth. Each verb builds its
//! command line as a pure function returning an [`OpsCommand`]; the executor
//! (or the `--dry-run` printer) consumes it, so argv construction stays
//! unit-testable with no Docker daemon.

use anyhow::{bail, Context, Result};

use crate::ops::{plain, require_on_path, run_capture, run_step, OpsCommand};

/// Dev-channel local-candidate filename probed by the artifact resolver.
pub const DEFAULT_COMPOSE_FILE: &str = "compose.dev.yaml";

/// The service endpoints the dev stack exposes, as committed in
/// `compose.dev.yaml`'s port mappings. Printed after `local up` so the operator
/// has the URLs in hand. Hardcoded to match the compose file (see the
/// `endpoints_match_compose_file` test, which asserts the file still maps them).
const ENDPOINTS: &[(&str, &str)] = &[
    ("AgentOS API", "http://localhost:28000"),
    ("AgentOS Console", "http://localhost:28080/?api=1"),
    ("Langfuse UI", "http://localhost:23000"),
    ("Postgres", "localhost:25432"),
    ("Valkey", "localhost:26379"),
    ("ClickHouse HTTP", "localhost:28123"),
    ("MinIO S3", "localhost:29000"),
    ("MinIO console", "localhost:29001"),
    ("OTel gRPC", "localhost:24317"),
    ("OTel HTTP", "localhost:24318"),
];

/// Flags shared by every `local` verb.
pub struct LocalOpts {
    pub file: String,
    pub dry_run: bool,
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

/// `docker compose -f <file> up -d --wait`.
pub fn up_command(o: &LocalOpts) -> OpsCommand {
    compose(&o.file, &["up", "-d", "--wait"])
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
    for (label, url) in ENDPOINTS {
        ui.kv(label, &ui.url(url));
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
        }
    }

    #[test]
    fn up_uses_detached_wait() {
        let cmd = up_command(&opts(DEFAULT_COMPOSE_FILE));
        assert_eq!(
            cmd.display(),
            "docker compose -f compose.dev.yaml up -d --wait"
        );
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
        let compose =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../compose.dev.yaml"))
                .expect("read compose.dev.yaml");
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
                ENDPOINTS.iter().any(|(_, url)| url.contains(host_port)),
                "ENDPOINTS missing {host_port} for {label}"
            );
        }
        // The console must be advertised in wired mode (?api=1); the published UI
        // image is fixture-by-default and only talks to the API when the URL
        // carries this param.
        let console = ENDPOINTS
            .iter()
            .find(|(label, _)| *label == "AgentOS Console")
            .expect("AgentOS Console endpoint present");
        assert!(
            console.1.contains("api=1"),
            "AgentOS Console endpoint must be the wired ?api=1 URL, got {}",
            console.1
        );
    }
}
