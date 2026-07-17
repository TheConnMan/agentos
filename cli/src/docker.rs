//! Local runner orchestration via the Docker CLI.
//!
//! `agentos skill up` boots the D1 runner image with the ACI-frozen boot env
//! (runner/README.md documents the recipe); `stop` tears it down. Shelling out
//! to `docker` keeps the CLI dependency-light: the target machine is a dev
//! laptop that already has Docker if it can run the runner at all.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use tokio::process::Command;

/// Everything `docker run` needs to boot a local runner container.
#[derive(Debug, Clone)]
pub struct StartSpec {
    pub image: String,
    pub container_name: String,
    pub host_port: u16,
    pub plugin_dir: PathBuf,
    pub session_id: String,
    pub sandbox_id: String,
    pub budget_json: String,
    pub fake_model: bool,
    pub network: Option<String>,
    pub otel_endpoint: Option<String>,
    pub model_base_url: Option<String>,
    /// Model id forwarded as `AGENTOS_MODEL`; `None` leaves the runner on its
    /// SDK default.
    pub model: Option<String>,
    /// Env vars forwarded from the caller's environment when set (model
    /// credentials for real-model runs; never baked into the args as values).
    pub passthrough_env: Vec<String>,
    /// Env values supplied only to the Docker CLI process. Used for secrets
    /// loaded from AgentOS private storage so they can be forwarded by `-e NAME`
    /// without mutating the AgentOS process env or appearing in argv.
    pub docker_env: Vec<(String, String)>,
}

/// Everything `docker run` needs for a one shot offline MCP load check.
#[derive(Debug, Clone)]
pub struct CheckSpec {
    pub image: String,
    pub plugin_dir: String,
    pub timeout_s: u64,
}

impl CheckSpec {
    /// The one shot check container argv (after the `docker` executable).
    pub fn run_args(&self) -> Vec<String> {
        vec![
            "run".into(),
            "--rm".into(),
            // Offline contract: the check must never reach the network. A bundle
            // with a remote (`url:`) MCP server, or a stdio server that phones
            // home at startup, must fail (red) rather than pass by connecting
            // out. `--network none` is empirically verified NOT to break the
            // legitimate in-bundle stdio-server case.
            "--network".into(),
            "none".into(),
            "-v".into(),
            format!("{}:/plugin:ro", self.plugin_dir),
            "-e".into(),
            "AGENTOS_PLUGIN_DIR=/plugin".into(),
            "-e".into(),
            format!("AGENTOS_CHECK_TIMEOUT_S={}", self.timeout_s),
            self.image.clone(),
            "python".into(),
            "-m".into(),
            "agentos_runner.check".into(),
        ]
    }
}

impl StartSpec {
    /// The `docker run` argument vector (after the `docker` executable).
    pub fn run_args(&self) -> Vec<String> {
        let mut args: Vec<String> = vec![
            "run".into(),
            "-d".into(),
            "--name".into(),
            self.container_name.clone(),
            "-p".into(),
            format!("{}:8080", self.host_port),
            "-v".into(),
            format!("{}:/plugin:ro", self.plugin_dir.display()),
            "-e".into(),
            "AGENTOS_PLUGIN_DIR=/plugin".into(),
            "-e".into(),
            format!("AGENTOS_SESSION_ID={}", self.session_id),
            "-e".into(),
            format!("AGENTOS_SANDBOX_ID={}", self.sandbox_id),
            "-e".into(),
            format!("AGENTOS_BUDGET={}", self.budget_json),
        ];
        if self.fake_model {
            args.push("-e".into());
            args.push("AGENTOS_FAKE_MODEL=1".into());
        }
        if let Some(model) = &self.model {
            args.push("-e".into());
            args.push(format!("AGENTOS_MODEL={model}"));
        }
        if let Some(url) = &self.model_base_url {
            args.push("-e".into());
            args.push(format!("ANTHROPIC_BASE_URL={url}"));
        }
        if let Some(network) = &self.network {
            args.push("--network".into());
            args.push(network.clone());
        }
        if let Some(endpoint) = &self.otel_endpoint {
            args.push("-e".into());
            args.push(format!("OTEL_EXPORTER_OTLP_ENDPOINT={endpoint}"));
        }
        for var in &self.passthrough_env {
            if std::env::var_os(var).is_some()
                || self.docker_env.iter().any(|(name, _)| name == var)
            {
                args.push("-e".into());
                args.push(var.clone());
            }
        }
        args.push(self.image.clone());
        args
    }
}

/// Run a docker subcommand, returning trimmed stdout; stderr on failure.
pub async fn docker(args: &[String]) -> Result<String> {
    docker_with_env(args, &[]).await
}

/// Run a docker subcommand with extra environment values supplied only to the
/// Docker CLI child process.
pub async fn docker_with_env(args: &[String], env: &[(String, String)]) -> Result<String> {
    let (status, stdout, stderr) = docker_capture_with_env(args, env).await?;
    if !status.success() {
        bail!(
            "docker {} failed ({}): {}",
            args.first().map(String::as_str).unwrap_or(""),
            status,
            stderr
        );
    }
    Ok(stdout)
}

/// Run a docker subcommand and capture its status plus both output streams.
///
/// A check container's nonzero verdict is data, so unlike [`docker`] this does
/// not turn an unsuccessful child exit into an error. Failure to invoke Docker
/// remains an error.
pub async fn docker_capture(args: &[String]) -> Result<(std::process::ExitStatus, String, String)> {
    docker_capture_with_env(args, &[]).await
}

pub async fn docker_capture_with_env(
    args: &[String],
    env: &[(String, String)],
) -> Result<(std::process::ExitStatus, String, String)> {
    let mut cmd = Command::new("docker");
    cmd.args(args);
    for (name, value) in env {
        cmd.env(name, value);
    }
    let output = cmd
        .output()
        .await
        .context("failed to invoke docker; is Docker installed and on PATH?")?;
    Ok((
        output.status,
        String::from_utf8_lossy(&output.stdout).trim().to_string(),
        String::from_utf8_lossy(&output.stderr).trim().to_string(),
    ))
}

/// Create a docker network. Returns `Ok(true)` when this call created it and
/// `Ok(false)` when the network already existed, so the caller only claims
/// ownership (and thus teardown responsibility) for networks it actually made.
pub async fn create_network(name: &str) -> Result<bool> {
    let output = Command::new("docker")
        .args(["network", "create", name])
        .output()
        .await
        .context("failed to invoke docker; is Docker installed and on PATH?")?;
    if output.status.success() {
        return Ok(true);
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    if stderr.contains("already exists") {
        return Ok(false);
    }
    bail!(
        "docker network create failed ({}): {}",
        output.status,
        stderr.trim()
    )
}

pub async fn remove_network(name: &str) -> Result<()> {
    docker(&["network".into(), "rm".into(), name.to_string()])
        .await
        .map(|_| ())
}

/// The named volume that persists an ollama container's model cache
/// (`/root/.ollama`) across `skill down`/`skill up`, so a repeat demo reuses
/// the pulled model instead of re-downloading it.
pub fn ollama_volume(container: &str) -> String {
    format!("{container}-data")
}

/// The `docker run` argument vector (after the `docker` executable) that boots
/// the ollama container. A named volume for `/root/.ollama` keeps the pulled
/// model cached across teardown; Docker auto-creates it on first use.
pub fn ollama_run_args(container: &str, network: &str, image: &str) -> Vec<String> {
    vec![
        "run".into(),
        "-d".into(),
        "--name".into(),
        container.to_string(),
        "--network".into(),
        network.to_string(),
        "-v".into(),
        format!("{}:/root/.ollama", ollama_volume(container)),
        image.to_string(),
    ]
}

pub async fn run_ollama(container: &str, network: &str, image: &str) -> Result<String> {
    docker(&ollama_run_args(container, network, image)).await
}

pub async fn wait_ollama_ready(container: &str, timeout: Duration) -> Result<()> {
    let started = Instant::now();
    loop {
        let output = Command::new("docker")
            .args(["exec", container, "ollama", "list"])
            .output()
            .await
            .context("failed to invoke docker; is Docker installed and on PATH?")?;
        if output.status.success() {
            return Ok(());
        }
        if started.elapsed() >= timeout {
            bail!(
                "ollama container '{container}' did not become ready within {}s: {}",
                timeout.as_secs(),
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub async fn pull_model(container: &str, model: &str) -> Result<()> {
    docker(&[
        "exec".into(),
        container.to_string(),
        "ollama".into(),
        "pull".into(),
        model.to_string(),
    ])
    .await
    .map(|_| ())
}

/// Best-effort container teardown (used for cleanup paths).
pub async fn remove_container(name_or_id: &str) -> Result<()> {
    docker(&["rm".into(), "-f".into(), name_or_id.to_string()])
        .await
        .map(|_| ())
}

/// The outcome of reaping labeled containers: what was removed, what is still
/// running, and whether the reap could be confirmed clean (#613).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReapReport {
    /// Containers `docker rm -f` confirmed removed (echoed on its stdout, #551).
    pub removed: usize,
    /// Labeled containers still present after the reap, established by re-listing
    /// rather than inferred from the `rm` exit. Non-empty means a failed or
    /// partial removal that must NOT be reported as a clean teardown.
    pub still_present: Vec<String>,
    /// A docker error that made the reap uncertain (the list step failed, the
    /// remove exited nonzero while containers remain, or the confirming re-list
    /// failed). `None` only when the teardown is confirmed clean.
    pub error: Option<String>,
}

async fn labeled_container_ids(label: &str) -> Result<Vec<String>> {
    let list_args: Vec<String> = vec![
        "ps".into(),
        "-a".into(),
        "--filter".into(),
        format!("label={label}"),
        "-q".into(),
    ];
    let out = docker(&list_args).await?;
    Ok(out
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect())
}

/// Assemble a [`ReapReport`] from the `rm` output and the post-`rm` re-list.
///
/// Pure so the disclosure logic (#613) is testable without a Docker daemon.
/// `still_present` is the ground truth for what is left running: a container
/// that raced away between the two lists is gone from both `removed` (it is not
/// echoed by `rm`, #551) and here, so it is neither over-counted nor falsely
/// reported as leftover. A nonzero `rm` exit whose containers are nonetheless
/// gone (the "No such container" of exactly that race) is therefore clean.
fn reap_report(
    rm_stdout: &str,
    rm_error: Option<String>,
    still_present: Vec<String>,
) -> ReapReport {
    let removed = count_removed(rm_stdout);
    let error = if still_present.is_empty() {
        None
    } else {
        Some(rm_error.unwrap_or_else(|| {
            format!(
                "{} container(s) still running after docker rm -f",
                still_present.len()
            )
        }))
    };
    ReapReport {
        removed,
        still_present,
        error,
    }
}

/// Remove all containers matching a Docker label filter, reporting what was and
/// was not removed.
///
/// Unlike the old best-effort version, neither the list step nor the removal
/// step is collapsed to a silent zero: a list failure, a nonzero `rm`, or a
/// confirming re-list failure is surfaced on [`ReapReport::error`] so a caller
/// (e.g. `local down`) cannot report a clean teardown while runner containers
/// keep holding ports and credentials (#613).
pub async fn reap_labeled(label: &str) -> ReapReport {
    let candidates = match labeled_container_ids(label).await {
        Ok(ids) => ids,
        Err(e) => {
            return ReapReport {
                removed: 0,
                still_present: Vec::new(),
                error: Some(format!("could not list containers labeled {label}: {e}")),
            }
        }
    };
    if candidates.is_empty() {
        return ReapReport {
            removed: 0,
            still_present: Vec::new(),
            error: None,
        };
    }

    let mut rm_args: Vec<String> = vec!["rm".into(), "-f".into()];
    rm_args.extend(candidates.iter().cloned());
    // Capture (not `docker`) so a nonzero exit keeps its stdout: `docker rm -f`
    // exits nonzero if ANY id fails, yet still echoes the ids it did remove.
    let (rm_stdout, rm_error) = match docker_capture_with_env(&rm_args, &[]).await {
        Ok((status, stdout, _)) if status.success() => (stdout, None),
        Ok((status, stdout, stderr)) => (
            stdout,
            Some(format!("docker rm -f exited {status}: {}", stderr.trim())),
        ),
        Err(e) => (
            String::new(),
            Some(format!("could not run docker rm -f: {e}")),
        ),
    };

    // Re-list for the ground truth of what is still running.
    match labeled_container_ids(label).await {
        Ok(still_present) => reap_report(&rm_stdout, rm_error, still_present),
        Err(e) => ReapReport {
            removed: count_removed(&rm_stdout),
            still_present: Vec::new(),
            error: Some(match rm_error {
                Some(r) => format!("{r}; also could not confirm teardown: {e}"),
                None => format!("could not confirm teardown of containers labeled {label}: {e}"),
            }),
        },
    }
}

/// The number of containers `docker rm -f` confirmed removed: it echoes one
/// removed id per stdout line. Pure so the teardown-count fix (#551) is testable
/// without a Docker daemon.
fn count_removed(rm_stdout: &str) -> usize {
    rm_stdout
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .count()
}

/// The label that worker-local stamps on every runner container it spawns.
pub const SANDBOX_LABEL: &str = "agentos.dev/managed-by=agentos-sandbox-substrate";

/// The runner image's short name (the dev/local tag, and the base of the GHCR
/// release ref). One definition (#497) instead of the literal scattered across
/// the artifact resolver, the boot path, and the clap defaults.
pub const RUNNER_IMAGE: &str = "agentos-runner";

/// The fixed container name for the single local/skill runner (`skill up` /
/// `local` boot it, `skill down` reaps it). One definition (#497).
pub const RUNNER_CONTAINER_LOCAL: &str = "agentos-runner-local";

/// The last log lines of a container, for boot-failure diagnostics.
pub async fn container_logs(name_or_id: &str, tail: u32) -> String {
    let args: Vec<String> = vec![
        "logs".into(),
        "--tail".into(),
        tail.to_string(),
        name_or_id.to_string(),
    ];
    match Command::new("docker").args(&args).output().await {
        Ok(output) => format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ),
        Err(err) => format!("(could not read container logs: {err})"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn count_removed_counts_actual_rm_output_not_the_candidate_set() {
        // `docker rm -f` echoes one removed id per line; that is the truthful
        // count. A container that vanished before rm simply is not echoed (#551).
        assert_eq!(count_removed("abc123\ndef456\n"), 2);
        // Trailing/blank lines and surrounding whitespace do not inflate it.
        assert_eq!(count_removed("  abc123  \n\n"), 1);
        // Nothing removed -> zero, not a phantom "removed 1".
        assert_eq!(count_removed(""), 0);
        assert_eq!(count_removed("\n   \n"), 0);
    }

    #[test]
    fn reap_report_clean_when_nothing_remains() {
        // Both candidates removed, re-list empty: a clean teardown, no error.
        let r = reap_report("abc123\ndef456\n", None, Vec::new());
        assert_eq!(r.removed, 2);
        assert!(r.still_present.is_empty());
        assert_eq!(r.error, None);
    }

    #[test]
    fn reap_report_discloses_a_partial_failure() {
        // rm exited nonzero and a container is still present: NOT a clean
        // teardown. The count reflects what was confirmed gone; the leftover and
        // the docker error are surfaced so `local down` cannot report success.
        let r = reap_report(
            "abc123\n",
            Some("docker rm -f exited 1: permission denied".into()),
            vec!["def456".into()],
        );
        assert_eq!(r.removed, 1);
        assert_eq!(r.still_present, vec!["def456".to_string()]);
        assert_eq!(
            r.error.as_deref(),
            Some("docker rm -f exited 1: permission denied")
        );
    }

    #[test]
    fn reap_report_treats_a_raced_away_container_as_clean() {
        // `docker rm -f` exited nonzero because a candidate vanished between the
        // list and the remove ("No such container"), but the re-list is empty:
        // nothing is actually left, so the teardown is clean despite the nonzero.
        let r = reap_report(
            "abc123\n",
            Some("docker rm -f exited 1: No such container: def456".into()),
            Vec::new(),
        );
        assert_eq!(r.removed, 1);
        assert!(r.still_present.is_empty());
        assert_eq!(r.error, None);
    }

    #[test]
    fn reap_report_flags_leftovers_even_without_an_rm_error() {
        // A container remains but `rm` reported success (e.g. it was recreated by
        // a restart policy): still disclosed with a generated message.
        let r = reap_report("abc123\n", None, vec!["ghi789".into()]);
        assert_eq!(r.removed, 1);
        assert_eq!(r.still_present, vec!["ghi789".to_string()]);
        assert!(r.error.unwrap().contains("still running"));
    }

    fn spec() -> StartSpec {
        StartSpec {
            image: "agentos-runner".into(),
            container_name: "agentos-runner-local".into(),
            host_port: 7245,
            plugin_dir: PathBuf::from("/tmp/deal-desk"),
            session_id: "local-1".into(),
            sandbox_id: "local".into(),
            budget_json: r#"{"max_output_tokens_per_run":100000,"max_usd_per_day":5.0}"#.into(),
            fake_model: true,
            network: Some("agentos_default".into()),
            otel_endpoint: Some("http://otel-collector:4318".into()),
            model_base_url: None,
            model: None,
            passthrough_env: vec!["AGENTOS_TEST_ENV_THAT_DOES_NOT_EXIST".into()],
            docker_env: vec![],
        }
    }

    #[test]
    fn ollama_run_args_mount_the_model_cache_volume() {
        let args = ollama_run_args("agentos-ollama", "agentos-net", "ollama/ollama:0.24.0");
        let joined = args.join(" ");
        assert!(joined.starts_with("run -d --name agentos-ollama --network agentos-net"));
        assert!(joined.contains("-v agentos-ollama-data:/root/.ollama"));
        assert_eq!(args.last().unwrap(), "ollama/ollama:0.24.0");
        assert_eq!(ollama_volume("agentos-ollama"), "agentos-ollama-data");
    }

    #[test]
    fn run_args_carry_the_aci_boot_env() {
        let args = spec().run_args();
        let joined = args.join(" ");
        assert!(joined.starts_with("run -d --name agentos-runner-local -p 7245:8080"));
        assert!(joined.contains("-v /tmp/deal-desk:/plugin:ro"));
        assert!(joined.contains("-e AGENTOS_PLUGIN_DIR=/plugin"));
        assert!(joined.contains("-e AGENTOS_SESSION_ID=local-1"));
        assert!(joined.contains("-e AGENTOS_SANDBOX_ID=local"));
        assert!(joined.contains(
            "-e AGENTOS_BUDGET={\"max_output_tokens_per_run\":100000,\"max_usd_per_day\":5.0}"
        ));
        assert!(joined.contains("-e AGENTOS_FAKE_MODEL=1"));
        assert!(joined.contains("--network agentos_default"));
        assert!(joined.contains("-e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318"));
        assert_eq!(args.last().unwrap(), "agentos-runner");
    }

    #[test]
    fn unset_passthrough_env_is_not_forwarded_and_real_model_omits_fake_flag() {
        let mut s = spec();
        s.fake_model = false;
        let joined = s.run_args().join(" ");
        assert!(!joined.contains("AGENTOS_FAKE_MODEL"));
        assert!(!joined.contains("AGENTOS_TEST_ENV_THAT_DOES_NOT_EXIST"));
    }

    #[test]
    fn docker_env_marks_passthrough_name_without_leaking_value() {
        let mut s = spec();
        s.passthrough_env = vec!["GITHUB_PERSONAL_ACCESS_TOKEN".into()];
        s.docker_env = vec![("GITHUB_PERSONAL_ACCESS_TOKEN".into(), "ghp-secret".into())];
        let joined = s.run_args().join(" ");
        assert!(joined.contains("-e GITHUB_PERSONAL_ACCESS_TOKEN"));
        assert!(!joined.contains("ghp-secret"));
    }

    #[test]
    fn model_is_forwarded_only_when_set() {
        let mut s = spec();
        s.model = None;
        assert!(!s.run_args().join(" ").contains("AGENTOS_MODEL"));

        s.model = Some("claude-opus-4-8".into());
        assert!(s
            .run_args()
            .join(" ")
            .contains("-e AGENTOS_MODEL=claude-opus-4-8"));
    }

    #[test]
    fn model_base_url_is_forwarded_when_set() {
        let mut s = spec();
        s.model_base_url = Some("http://x-ollama:11434".into());
        assert!(s
            .run_args()
            .join(" ")
            .contains("-e ANTHROPIC_BASE_URL=http://x-ollama:11434"));
    }

    #[test]
    fn model_base_url_is_omitted_when_unset() {
        let mut s = spec();
        s.model_base_url = None;
        assert!(!s.run_args().join(" ").contains("ANTHROPIC_BASE_URL"));
    }

    #[test]
    fn real_model_with_model_base_url_still_omits_fake_flag() {
        let mut s = spec();
        s.fake_model = false;
        s.model_base_url = Some("http://x-ollama:11434".into());
        let joined = s.run_args().join(" ");
        assert!(joined.contains("-e ANTHROPIC_BASE_URL=http://x-ollama:11434"));
        assert!(!joined.contains("AGENTOS_FAKE_MODEL"));
    }
}
