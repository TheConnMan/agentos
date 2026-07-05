//! Command handlers behind the `agentos` subcommands.
//!
//! main.rs owns the clap surface; each handler here owns one subcommand's
//! behavior and speaks only through the library modules (docker, runner, api,
//! scaffold, state, evals, render).

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use agentos_aci_protocol::{Budget, EventType, OutboundEvent, SessionStatus};
use anyhow::{bail, Context, Result};
use clap::ValueEnum;

use crate::api::ApiClient;
use crate::bundle::pack_tar_gz;
use crate::docker::{self, StartSpec};
use crate::evals::{case_line, load_cases, summary_line, turn_passes};
use crate::render::{boxed_summary, TurnPrinter};
use crate::runner::RunnerClient;
use crate::scaffold::{read_manifest, scaffold};
use crate::state::{self, RunnerState};

pub const DEFAULT_PORT: u16 = 7245; // the design canon's local bot port
pub const DEFAULT_BUDGET: &str = r#"{"max_output_tokens_per_run":100000,"max_usd_per_day":5.0}"#;

#[derive(Clone, Copy, ValueEnum)]
pub enum SendType {
    Message,
    Job,
    EvalCase,
}

impl From<SendType> for EventType {
    fn from(value: SendType) -> Self {
        match value {
            SendType::Message => EventType::Message,
            SendType::Job => EventType::Job,
            SendType::EvalCase => EventType::EvalCase,
        }
    }
}

#[derive(Clone, Copy, ValueEnum)]
pub enum DeployEnv {
    Dev,
    Prod,
}

impl DeployEnv {
    pub fn as_str(self) -> &'static str {
        match self {
            DeployEnv::Dev => "dev",
            DeployEnv::Prod => "prod",
        }
    }
}

/// Options for `agentos start`, mirroring its clap flags.
pub struct StartOpts {
    pub plugin_dir: PathBuf,
    pub image: String,
    pub port: u16,
    pub name: String,
    pub fake_model: bool,
    pub network: Option<String>,
    pub otel_endpoint: Option<String>,
    pub budget: String,
}

pub fn init(name: &str, dir: Option<PathBuf>) -> Result<()> {
    let dir = dir.unwrap_or_else(|| PathBuf::from(name));
    let created = scaffold(&dir, name)?;
    println!("Initialized plugin bundle '{name}' in {}", dir.display());
    for path in created {
        println!("  created {}", path.display());
    }
    println!("\nNext: cd {} && agentos start", dir.display());
    Ok(())
}

pub async fn start(opts: StartOpts) -> Result<()> {
    let plugin_dir = opts
        .plugin_dir
        .canonicalize()
        .with_context(|| format!("plugin dir not found: {}", opts.plugin_dir.display()))?;
    // Fail fast on a directory that is not a bundle; the runner would reject
    // it at boot anyway (real-model mode), with a worse error surface.
    let (plugin_name, manifest_version) = read_manifest(&plugin_dir)?;

    if state::load(&plugin_dir)?.is_some() {
        bail!(
            "a local runner is already recorded in {}/.agentos/runner.json; run 'agentos stop' there first",
            plugin_dir.display()
        );
    }

    // Parse (not just forward) the budget so a typo fails here, not in-container.
    let _: Budget = serde_json::from_str(&opts.budget)
        .with_context(|| format!("--budget is not a valid ACI budget: {}", opts.budget))?;

    let session_id = format!("local-{}", unix_now());
    let spec = StartSpec {
        image: opts.image.clone(),
        container_name: opts.name.clone(),
        host_port: opts.port,
        plugin_dir: plugin_dir.clone(),
        session_id: session_id.clone(),
        sandbox_id: "local".into(),
        budget_json: opts.budget,
        fake_model: opts.fake_model,
        network: opts.network,
        otel_endpoint: opts.otel_endpoint,
        passthrough_env: vec!["CLAUDE_CODE_OAUTH_TOKEN".into(), "ANTHROPIC_API_KEY".into()],
    };

    println!(
        "Starting runner container '{}' from image '{}'...",
        opts.name, opts.image
    );
    let container_id = docker::docker(&spec.run_args()).await?;

    let base_url = format!("http://localhost:{}", opts.port);
    let client = RunnerClient::new(&base_url)?;
    if let Err(err) = client.wait_healthy(Duration::from_secs(60)).await {
        let logs = docker::container_logs(&opts.name, 40).await;
        let _ = docker::remove_container(&opts.name).await;
        bail!("runner failed to become healthy: {err}\ncontainer logs:\n{logs}");
    }

    // State lives with the bundle: init gitignores .agentos/ there, and the
    // follow-up commands are documented to run from the bundle directory.
    state::save(
        &plugin_dir,
        &RunnerState {
            container_id,
            container_name: opts.name,
            image: opts.image,
            port: opts.port,
            base_url: base_url.clone(),
            session_id,
            plugin_dir: plugin_dir.display().to_string(),
            fake_model: opts.fake_model,
        },
    )?;

    let version = git_short_sha(&plugin_dir)
        .await
        .map(|sha| format!("dev @ {sha}"))
        .unwrap_or_else(|| format!("{plugin_name} @ {manifest_version}"));
    let rows = [
        ("Local bot", base_url),
        ("Slack emulator", "agentos send \"<message>\"".to_string()),
        ("Eval runner", "agentos eval".to_string()),
        ("Version", version),
    ];
    println!("{}", boxed_summary("agentos dev environment", &rows));
    let cwd = Path::new(".").canonicalize()?;
    if cwd != plugin_dir {
        println!(
            "\nState recorded in {}/.agentos/runner.json; run send/eval/stop from there (or pass --url).",
            plugin_dir.display()
        );
    }
    Ok(())
}

pub async fn stop() -> Result<()> {
    let dir = Path::new(".");
    let Some(saved) = state::load(dir)? else {
        bail!("no local runner recorded in .agentos/runner.json; run from the bundle directory");
    };
    match docker::remove_container(&saved.container_name).await {
        Ok(()) => println!("Stopped and removed container '{}'", saved.container_name),
        // The container being gone already is a success for stop: clear the
        // state instead of wedging start/stop on a stale runner.json.
        Err(err) if err.to_string().contains("No such container") => {
            println!(
                "Container '{}' was already gone; cleared stale state",
                saved.container_name
            );
        }
        Err(err) => return Err(err),
    }
    state::remove(dir)?;
    Ok(())
}

pub async fn status() -> Result<()> {
    let url = resolve_url(None)?;
    let client = RunnerClient::new(&url)?;
    let status = client.status().await?;
    println!("runner {url}");
    println!("{}", serde_json::to_string_pretty(&status)?);
    Ok(())
}

pub async fn send(
    text: &str,
    user: &str,
    event_type: EventType,
    url: Option<String>,
) -> Result<()> {
    let url = resolve_url(url)?;
    let client = RunnerClient::new(&url)?;
    let mut printer = TurnPrinter::default();
    let events = client
        .send_event(event_type, text, user, |event| {
            if let Some(line) = printer.line_for(event) {
                println!("{line}");
            }
        })
        .await?;

    if let Some(OutboundEvent::Final { status, .. }) = events.last() {
        if *status == SessionStatus::ClassifiedFailure {
            std::process::exit(1);
        }
    }
    Ok(())
}

pub async fn eval(cases_path: Option<PathBuf>, url: Option<String>) -> Result<()> {
    let state_plugin_dir = state::load(Path::new("."))?.map(|s| PathBuf::from(s.plugin_dir));
    let cases_path = resolve_cases_path(cases_path, Path::new("."), state_plugin_dir.as_deref())?;
    let cases = load_cases(&cases_path)?;
    let url = resolve_url(url)?;
    let client = RunnerClient::new(&url)?;

    let total = cases.len();
    let mut passed = 0usize;
    for case in &cases {
        let started = Instant::now();
        let events = client
            .send_event(EventType::EvalCase, &case.input, "U-eval", |_| {})
            .await?;
        let elapsed = started.elapsed().as_secs_f64();
        let ok = turn_passes(case, &events);
        if ok {
            passed += 1;
        }
        println!("{}", case_line(&case.name, ok, elapsed));
    }
    println!("{}", summary_line(passed, total));
    if passed < total {
        std::process::exit(1);
    }
    Ok(())
}

/// Where the eval cases live: an explicit `--cases` wins; otherwise
/// `evals/cases.json` in the current directory, falling back to the started
/// runner's recorded bundle directory (so `agentos eval` works from wherever
/// `agentos start` was run).
pub fn resolve_cases_path(
    explicit: Option<PathBuf>,
    cwd: &Path,
    state_plugin_dir: Option<&Path>,
) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path);
    }
    let local = cwd.join("evals/cases.json");
    if local.is_file() {
        return Ok(local);
    }
    if let Some(plugin_dir) = state_plugin_dir {
        let in_bundle = plugin_dir.join("evals/cases.json");
        if in_bundle.is_file() {
            return Ok(in_bundle);
        }
    }
    bail!(
        "no eval cases found: looked for {} and the running bundle's evals/cases.json; pass --cases",
        local.display()
    )
}

pub struct DeployOpts {
    pub plugin_dir: PathBuf,
    pub api_url: String,
    pub api_key: String,
    pub slack_channel: String,
    pub env: DeployEnv,
    pub label: Option<String>,
}

pub async fn deploy(opts: DeployOpts) -> Result<()> {
    let plugin_dir = opts
        .plugin_dir
        .canonicalize()
        .with_context(|| format!("plugin dir not found: {}", opts.plugin_dir.display()))?;
    let (plugin_name, manifest_version) = read_manifest(&plugin_dir)?;
    let label = opts
        .label
        .unwrap_or_else(|| format!("{manifest_version}-{}", unix_now()));
    let created_by = std::env::var("USER").unwrap_or_else(|_| "agentos-cli".to_string());

    let archive = pack_tar_gz(&plugin_dir)?;
    println!(
        "Deploying '{plugin_name}' ({} bytes) as {label} to {} [{}]",
        archive.len(),
        opts.api_url,
        opts.env.as_str()
    );

    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let outcome = client
        .deploy(
            &plugin_name,
            &opts.slack_channel,
            &label,
            &created_by,
            opts.env.as_str(),
            archive,
        )
        .await?;

    println!("agent       {} ({})", outcome.agent.name, outcome.agent.id);
    println!(
        "version     {} ({})",
        outcome.version.version_label, outcome.version.id
    );
    println!(
        "bundle      {} sha256:{} {} bytes",
        outcome.bundle.bundle_ref, outcome.bundle.bundle_sha256, outcome.bundle.size_bytes
    );
    println!(
        "deployment  {} [{}] {}",
        outcome.deployment.id, outcome.deployment.environment, outcome.deployment.status
    );
    Ok(())
}

fn resolve_url(explicit: Option<String>) -> Result<String> {
    if let Some(url) = explicit {
        return Ok(url);
    }
    if let Some(saved) = state::load(Path::new("."))? {
        return Ok(saved.base_url);
    }
    Ok(format!("http://localhost:{DEFAULT_PORT}"))
}

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock is after the epoch")
        .as_secs()
}

/// Short git SHA of the plugin dir's checkout, for the version line.
async fn git_short_sha(dir: &Path) -> Option<String> {
    let output = tokio::process::Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .current_dir(dir)
        .output()
        .await
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!sha.is_empty()).then_some(sha)
}

#[cfg(test)]
mod tests {
    use super::resolve_cases_path;
    use std::path::PathBuf;

    #[test]
    fn explicit_cases_path_wins() {
        let path = resolve_cases_path(
            Some(PathBuf::from("/x/cases.json")),
            std::path::Path::new("/nowhere"),
            None,
        )
        .unwrap();
        assert_eq!(path, PathBuf::from("/x/cases.json"));
    }

    #[test]
    fn falls_back_from_cwd_to_the_recorded_bundle_dir() {
        let cwd = tempfile::tempdir().unwrap();
        let bundle = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(bundle.path().join("evals")).unwrap();
        std::fs::write(bundle.path().join("evals/cases.json"), "[]").unwrap();

        // cwd has no cases: resolve into the bundle dir from the state file.
        let resolved = resolve_cases_path(None, cwd.path(), Some(bundle.path())).unwrap();
        assert_eq!(resolved, bundle.path().join("evals/cases.json"));

        // cwd cases take precedence once present.
        std::fs::create_dir_all(cwd.path().join("evals")).unwrap();
        std::fs::write(cwd.path().join("evals/cases.json"), "[]").unwrap();
        let resolved = resolve_cases_path(None, cwd.path(), Some(bundle.path())).unwrap();
        assert_eq!(resolved, cwd.path().join("evals/cases.json"));
    }

    #[test]
    fn errors_when_nothing_is_found() {
        let cwd = tempfile::tempdir().unwrap();
        let err = resolve_cases_path(None, cwd.path(), None).unwrap_err();
        assert!(err.to_string().contains("--cases"), "{err}");
    }
}
