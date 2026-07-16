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
use serde::{Deserialize, Serialize};

use crate::api::{ApiClient, BudgetConfig, ChannelOutcome};
use crate::bundle::pack_tar_gz;
use crate::docker::{self, CheckSpec, StartSpec};
use crate::evals::{load_suite, turn_passes, EvalSuite};
use crate::render::{boxed_summary, status_str, TurnPart, TurnPrinter};
use crate::runner::RunnerClient;
use crate::scaffold::{read_declared_secrets, read_manifest, scaffold, scaffold_from_spec};
use crate::state::{self, RunnerState};

pub const DEFAULT_PORT: u16 = 7245; // the design canon's local bot port
pub const DEFAULT_BUDGET: &str = r#"{"max_output_tokens_per_run":100000,"max_usd_per_day":5.0}"#;
pub const DEFAULT_LOCAL_MODEL: &str = "qwen3:4b";
pub const DEFAULT_OLLAMA_IMAGE: &str = "ollama/ollama:0.24.0";
pub const OLLAMA_PORT: u16 = 11434;

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

/// Options for `agentos skill up`, mirroring its clap flags.
pub struct StartOpts {
    pub plugin_dir: PathBuf,
    pub image: String,
    pub port: u16,
    pub name: String,
    pub fake_model: bool,
    pub network: Option<String>,
    pub otel_endpoint: Option<String>,
    pub budget: String,
    pub model: Option<String>,
    pub local_model: Option<String>,
    /// Extra env var NAMES to forward by name into the runner sandbox, for a
    /// bundle's authed MCP server to read a secret. Forwarded exactly like the
    /// model credentials (docker reads the value from the caller's env; the
    /// value never appears in argv). From `skill up --secret <NAME>`.
    pub secret: Vec<String>,
}

/// The versioned report emitted by `agentos_runner.check`.
#[derive(Debug, Deserialize, Serialize)]
pub struct CheckReport {
    pub check: String,
    pub version: u64,
    pub plugin_dir: String,
    pub declared: Vec<DeclaredServer>,
    /// Opaque pass-through of the runner's registered-server list. Never read by
    /// the human render (only round-tripped through `--json`), so it is kept as
    /// raw JSON: it round-trips losslessly and can never fail `parse_check_report`
    /// on a future tool/server shape.
    pub registered: Vec<serde_json::Value>,
    pub matches: Vec<CheckMatch>,
    pub verdict: String,
    pub reasons: Vec<String>,
    pub hints: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct DeclaredServer {
    pub name: String,
    pub source: String,
    pub form: String,
    /// True when the server carries a credential (env/headers) the credential-free
    /// offline check never exercised. `#[serde(default)]` keeps older reports that
    /// predate the field parsing (they default to false).
    #[serde(default)]
    pub authed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct CheckMatch {
    pub declared: String,
    pub registered: Option<String>,
    pub connected: bool,
    pub tool_count: u64,
}

/// Parse the frozen runner to CLI check report contract.
pub fn parse_check_report(stdout: &str) -> Result<CheckReport> {
    let report: CheckReport = serde_json::from_str(stdout)
        .context("runner check output is not valid JSON for the check report contract")?;
    if report.version != 1 {
        bail!(
            "runner check report contract version {} is unsupported; expected version 1",
            report.version
        );
    }
    Ok(report)
}

/// Map a runner check verdict to the CLI semantic exit contract.
pub fn check_outcome(report: &CheckReport) -> std::result::Result<(), crate::exit::CliError> {
    match report.verdict.as_str() {
        "green" => Ok(()),
        "red" => Err(crate::exit::CliError {
            message: "MCP load check reported red".into(),
            fix: None,
            class: crate::exit::ExitClass::Failure,
        }
        // A structurally bad bundle is `invalid_bundle` (the runner's `run_check`
        // rejects it at step 1), so every remaining red cause is a runtime one:
        // a declared server that never registered or failed to start, one that
        // registered zero tools, one that needs a credential the offline check
        // never forwards, or MCP init exceeding the deadline. The printed
        // `reason:` lines say which, so point at them rather than guess.
        .with_fix(
            "read the printed reason(s): fix the server's command/args, forward its credential with agentos skill up --secret <NAME>, or raise --timeout if MCP init ran long",
        )),
        "invalid_bundle" => {
            // An invalid bundle is a deterministic input error (exit 2, Usage),
            // matching the runner's own `check.py` exit-2 for this verdict: the
            // bundle dir exists but fails structural validation, so retrying the
            // same argv fails identically. Surface the structural `reasons` so
            // the user sees WHY the bundle is invalid.
            let mut message = String::from("MCP load check reported an invalid bundle");
            if !report.reasons.is_empty() {
                message.push_str(": ");
                message.push_str(&report.reasons.join("; "));
            }
            Err(crate::exit::CliError::usage(message).with_fix(
                "fix the reported bundle-structure errors (.claude-plugin/plugin.json and skills/) and run agentos skill check again",
            ))
        }
        verdict => Err(crate::exit::CliError {
            message: format!("MCP load check reported unknown verdict '{verdict}'"),
            fix: None,
            class: crate::exit::ExitClass::Failure,
        }),
    }
}

/// Run the offline MCP load check for a plugin bundle.
pub async fn check(plugin_dir: PathBuf, image: String, timeout_s: u64) -> Result<()> {
    let requested_dir = plugin_dir.display().to_string();
    let plugin_dir = plugin_dir.canonicalize().map_err(|err| {
        crate::exit::CliError::usage(format!("plugin dir not found: {requested_dir}: {err}"))
    })?;
    read_manifest(&plugin_dir).map_err(|err| {
        crate::exit::CliError::usage(format!("plugin dir is not a usable bundle: {err}"))
    })?;

    let spec = CheckSpec {
        image,
        plugin_dir: plugin_dir.display().to_string(),
        timeout_s,
    };
    let (status, stdout, stderr) = docker::docker_capture(&spec.run_args()).await?;
    // A container that DID run and produced parseable JSON is data (a
    // green/red/invalid verdict) regardless of its exit code. Only when the
    // stdout is NOT a valid report is this a real docker failure -- surface the
    // captured stderr (e.g. "Cannot connect to the Docker daemon") so the true
    // cause is visible instead of being dropped. Stays a plain Failure (exit 1);
    // Transient/exit 3 is reserved for reqwest connect/timeout errors (#323).
    let report = parse_check_report(&stdout).map_err(|err| {
        anyhow::anyhow!(
            "runner check output violated the check report contract: {err}; \
             docker exited {status}; stdout: {stdout}; stderr: {stderr}"
        )
    })?;

    let ui = crate::ui::ui();
    if ui.json() {
        ui.emit_json(&serde_json::to_value(&report)?);
    } else {
        let mut lines = vec![format!("declared: {}", report.declared.len())];
        lines.extend(report.matches.iter().map(|entry| {
            format!(
                "match: {} -> {} (connected: {}, tools: {})",
                entry.declared,
                entry.registered.as_deref().unwrap_or("none"),
                entry.connected,
                entry.tool_count
            )
        }));
        lines.push(format!("verdict: {}", report.verdict));
        lines.extend(
            report
                .reasons
                .iter()
                .map(|reason| format!("reason: {reason}")),
        );
        lines.extend(report.hints.iter().map(|hint| format!("hint: {hint}")));
        ui.payload_plain(&lines.join("\n"));
    }

    check_outcome(&report).map_err(anyhow::Error::from)
}

pub fn init(name: Option<String>, dir: Option<PathBuf>, from_spec: Option<PathBuf>) -> Result<()> {
    let ui = crate::ui::ui();

    // Spec-file path (ADR-0021 decision 5): fully non-interactive. The bundle
    // name comes from the spec, never a prompt.
    if let Some(spec_path) = from_spec {
        let body = std::fs::read_to_string(&spec_path)
            .with_context(|| format!("reading spec file {}", spec_path.display()))?;
        let spec = crate::spec::parse(&body)?;
        // A positional name is allowed only if it matches the spec's name; a
        // mismatch is an authoring error, not a silent override.
        if let Some(positional) = &name {
            if positional != &spec.name {
                bail!(
                    "positional name {:?} does not match the spec name {:?}; \
                     the bundle name comes from the spec -- omit the name or make them match",
                    positional,
                    spec.name
                );
            }
        }
        let dir = dir.unwrap_or_else(|| PathBuf::from(&spec.name));
        let created = scaffold_from_spec(&dir, &spec)?;
        report_scaffold(
            ui,
            format!(
                "initialized plugin bundle '{}' in {} (from spec {})",
                spec.name,
                dir.display(),
                spec_path.display()
            ),
            created,
            &dir,
        );
        return Ok(());
    }

    let name = match name {
        Some(name) => name,
        None => bail!("provide a plugin NAME or --from-spec <path>"),
    };
    let dir = dir.unwrap_or_else(|| PathBuf::from(&name));
    let created = scaffold(&dir, &name)?;
    report_scaffold(
        ui,
        format!("initialized plugin bundle '{name}' in {}", dir.display()),
        created,
        &dir,
    );
    Ok(())
}

/// Report a freshly scaffolded bundle: the success line, one `created` note per
/// written path, and the `Next:` hint. Shared by both `init` branches so the
/// only per-branch difference is the success message text.
fn report_scaffold(ui: &crate::ui::Ui, success_msg: String, created: Vec<PathBuf>, dir: &Path) {
    ui.success(&success_msg);
    for path in created {
        ui.note(&format!("created {}", path.display()));
    }
    ui.note(&format!("Next: cd {} && agentos skill up", dir.display()));
}

/// `agentos build`: build the runner image locally from the repo's Dockerfile.
/// The one-command equivalent of `docker build -f runner/Dockerfile -t <tag> .`
/// run from the repo root. Errors clearly when Docker is missing or when run
/// outside a source checkout (a release binary pulls the image from GHCR).
pub async fn build(tag: &str) -> Result<()> {
    let ui = crate::ui::ui();
    if !on_path("docker") {
        bail!(
            "Docker is not installed or not on PATH. Install Docker \
             (https://docs.docker.com/get-docker/) and retry."
        );
    }
    let root = find_repo_root().context(
        "runner/Dockerfile not found here or in any parent directory. Run `agentos build` \
         from an agentos repo checkout -- a release binary pulls the runner image from GHCR \
         automatically and never needs to build.",
    )?;
    ui.note(&format!(
        "=== docker build -f runner/Dockerfile -t {tag} . (in {}) ===",
        root.display()
    ));
    // Inherit stdio so the build log streams to the terminal like a hand-run build.
    let status = tokio::process::Command::new("docker")
        .args(["build", "-f", "runner/Dockerfile", "-t", tag, "."])
        .current_dir(&root)
        .status()
        .await
        .context("failed to invoke docker")?;
    if !status.success() {
        bail!("docker build failed ({status})");
    }
    ui.success(&format!("built runner image '{tag}'"));
    Ok(())
}

/// `agentos install`: from-a-checkout dev bootstrap/update -- install deps and
/// build the runner image, but start nothing. Each step is idempotent and
/// streams its output; update mode reuses already-present heavyweight artifacts.
/// A missing tool prints a friendly pointer and stops. A release binary has no
/// source tree to install, so this errors clearly outside a checkout.
pub async fn install(update: bool) -> Result<()> {
    let ui = crate::ui::ui();
    let root = find_repo_root().context(
        "runner/Dockerfile not found here or in any parent directory. Run `agentos install` \
         from an agentos source checkout -- a release binary has nothing to install.",
    )?;

    // 1. Local config is user-owned. It is gitignored and only created once,
    // so pulling newer AgentOS sources and rerunning install cannot replace it.
    match seed_env_if_missing(&root)? {
        EnvSeed::Preserved => ui.note("=== .env already exists; leaving it untouched ==="),
        EnvSeed::Created => ui.note("=== seeded .env from .env.example ==="),
        EnvSeed::NoTemplate => ui.note("=== no .env.example to seed .env from; skipping ==="),
    }

    // 2. uv sync (repo root).
    require_tool("uv", "uv is not installed - https://docs.astral.sh/uv/")?;
    run_step(&root, "uv", &["sync"], "uv sync").await?;

    // 3. pnpm install in apps/ui.
    require_tool(
        "pnpm",
        "pnpm is not installed - https://pnpm.io/installation",
    )?;
    run_step(
        &root.join("apps/ui"),
        "pnpm",
        &["install"],
        "pnpm install (apps/ui)",
    )
    .await?;

    // 4. cargo install the CLI onto PATH (~/.cargo/bin), not just `cargo build`
    // into target/debug. `install` should make the CLI it builds LIVE -- like
    // `npm i` reconciling to the manifest -- so re-running it after a code change
    // refreshes what the user actually runs, instead of silently leaving a stale
    // on-PATH binary. `agentos update` is the fast CLI-only subset of this.
    require_tool("cargo", "cargo is not installed - https://rustup.rs/")?;
    run_step(
        &root,
        "cargo",
        &["install", "--path", "cli", "--force"],
        "cargo install (cli -> ~/.cargo/bin)",
    )
    .await?;

    // 5. Build the runner image via the existing `build` handler. Update mode
    // keeps reruns quick when the image is already present locally.
    let runner_image = "agentos-runner";
    if update && docker_image_exists(runner_image).await? {
        ui.note(&format!(
            "=== runner image '{runner_image}' already exists; skipping rebuild for --update ==="
        ));
    } else {
        build(runner_image).await?;
    }

    ui.success("Setup complete. Start the stack with: agentos local up");
    Ok(())
}

/// `agentos update`: rebuild the CLI from this source checkout and reinstall it
/// on PATH (`cargo install --path cli --force` -> ~/.cargo/bin), so a code change
/// is picked up on the next `agentos` invocation without re-running the bootstrap
/// script. Optionally rebuilds the local runner image too. Source-checkout only,
/// like `install` -- a release binary has no source to rebuild from. Replacing the
/// running binary is safe: the current process keeps running from the old inode
/// and the next invocation is the freshly installed one.
pub async fn update(image: bool) -> Result<()> {
    let ui = crate::ui::ui();
    // `update` rebuilds from a source checkout; a release-installed binary has no
    // checkout to rebuild from. Point that user at the release assets instead of
    // the generic install error, and be explicit that self-update-from-release is
    // not built here (#443 review).
    let root = find_repo_root().ok_or_else(|| {
        crate::exit::usage(
            "`agentos update` rebuilds the CLI from a source checkout, but this binary is not \
             running inside one.\n  - From a git clone: run `agentos update` from the repo.\n  \
             - Installed from a GitHub release: download the latest agentos-<target> asset from \
             https://github.com/curie-eng/agentos/releases and replace this binary (updating a \
             released binary from the latest release is not built yet).",
        )
    })?;
    require_tool("cargo", "cargo is not installed - https://rustup.rs/")?;
    run_step(
        &root,
        "cargo",
        &["install", "--path", "cli", "--force"],
        "cargo install (cli -> ~/.cargo/bin)",
    )
    .await?;
    if image {
        build("agentos-runner").await?;
    }
    ui.success("agentos updated. The new binary is live on your next `agentos` invocation.");
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EnvSeed {
    Preserved,
    Created,
    NoTemplate,
}

fn seed_env_if_missing(root: &Path) -> Result<EnvSeed> {
    let env_path = root.join(".env");
    if env_path.exists() {
        return Ok(EnvSeed::Preserved);
    }
    let env_example = root.join(".env.example");
    if !env_example.exists() {
        return Ok(EnvSeed::NoTemplate);
    }
    std::fs::copy(&env_example, &env_path).context("failed to copy .env.example to .env")?;
    Ok(EnvSeed::Created)
}

/// `agentos dev <script>`: run a repo dev script by relative path. Thin wrapper
/// -- finds the repo root, confirms the script exists, shells `bash <script>`
/// from the root, streams its output, and propagates its exit code. A release
/// binary has no scripts, so this errors clearly outside a checkout.
pub async fn dev_script(rel_path: &str) -> Result<()> {
    let ui = crate::ui::ui();
    let root = find_repo_root().context(
        "runner/Dockerfile not found here or in any parent directory. Run `agentos dev` \
         from an agentos source checkout -- a release binary has no dev scripts.",
    )?;
    let script = root.join(rel_path);
    if !script.is_file() {
        bail!("script not found: {}", script.display());
    }
    ui.note(&format!("=== bash {rel_path} (in {}) ===", root.display()));
    let status = tokio::process::Command::new("bash")
        .arg(rel_path)
        .current_dir(&root)
        .status()
        .await
        .context("failed to invoke bash")?;
    if !status.success() {
        bail!("{rel_path} failed ({status})");
    }
    Ok(())
}

/// Bail with a friendly pointer when a required tool is not on PATH.
fn require_tool(bin: &str, hint: &str) -> Result<()> {
    if on_path(bin) {
        Ok(())
    } else {
        bail!("{hint}")
    }
}

/// Run one install step in `dir`, streaming its output and failing on nonzero.
async fn run_step(dir: &Path, bin: &str, args: &[&str], label: &str) -> Result<()> {
    let ui = crate::ui::ui();
    ui.note(&format!("=== {label} (in {}) ===", dir.display()));
    let status = tokio::process::Command::new(bin)
        .args(args)
        .current_dir(dir)
        .status()
        .await
        .with_context(|| format!("failed to invoke {bin}"))?;
    if !status.success() {
        bail!("{label} failed ({status})");
    }
    Ok(())
}

async fn docker_image_exists(tag: &str) -> Result<bool> {
    require_tool(
        "docker",
        "Docker is not installed or not on PATH. Install Docker Desktop/Engine and retry.",
    )?;
    let status = tokio::process::Command::new("docker")
        .args(["image", "inspect", tag])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .await
        .context("failed to invoke docker")?;
    Ok(status.success())
}

/// Whether `bin` resolves on PATH.
fn on_path(bin: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(bin).is_file()))
        .unwrap_or(false)
}

/// Walk up from the current directory to the repo root: the nearest ancestor
/// that contains `runner/Dockerfile`.
fn find_repo_root() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        if dir.join("runner/Dockerfile").is_file() {
            return Some(dir);
        }
        if !dir.pop() {
            return None;
        }
    }
}

/// Pick the model-credential env vars to forward into the runner container, BY
/// NAME (docker reads their values from the caller's env; no secret is put in
/// argv). Mirrors the worker docker substrate's positive single-credential
/// selection (apps/worker/src/agentos_worker/sandbox/docker.py):
/// - fake/local model (`suppress_credential`): forward NONE -- those runners
///   resolve no Anthropic credential, and a real token must not sit in an
///   untrusted, egress-rail-less container readable via /proc/1/environ.
/// - an explicit non-empty AGENTOS_CREDENTIALS (`byo_credential`): the operator's
///   chosen BYO credential, forwarded ALONE so an ambient SDK token can neither
///   shadow it nor ride into the sandbox.
/// - otherwise: the ambient SDK creds for the legacy real-Anthropic path.
fn select_passthrough_env(suppress_credential: bool, byo_credential: Option<&str>) -> Vec<String> {
    if suppress_credential {
        return Vec::new();
    }
    match byo_credential {
        Some(cred) if !cred.is_empty() => vec!["AGENTOS_CREDENTIALS".into()],
        _ => vec!["CLAUDE_CODE_OAUTH_TOKEN".into(), "ANTHROPIC_API_KEY".into()],
    }
}

/// Append `--secret` env var NAMES to the model-credential passthrough list,
/// de-duplicating. Unlike the model credential these are NOT suppressed under a
/// fake/local model run: a bundle's authed MCP server needs its token
/// regardless of which model drives the session. Names already present (a user
/// passing a model-credential var as a secret) are not duplicated.
fn merge_secret_env(mut passthrough: Vec<String>, secrets: &[String]) -> Vec<String> {
    for name in secrets {
        if !passthrough.contains(name) {
            passthrough.push(name.clone());
        }
    }
    passthrough
}

/// Is `name` exported with a usable value?
///
/// An empty-string credential is absent, not supplied (issue #540): `var_os`
/// alone reports `NAME=""` as present, which would suppress the vault fallback
/// and forward nothing usable. Mirrors `ops.rs::resolve_up_credentials` and
/// `interactive.rs::env_credential_present`.
fn env_credential_present(name: &str) -> bool {
    std::env::var(name).is_ok_and(|value| !value.is_empty())
}

fn secret_store_env(name: &str) -> Result<Option<(String, String)>> {
    if env_credential_present(name) {
        return Ok(None);
    }
    if !crate::secrets::is_saved(name)? {
        return Ok(None);
    }
    if let Some(value) = crate::secrets::get_value(name)? {
        crate::ui::ui().note(&format!(
            "{name}: loaded from AgentOS private storage for this run"
        ));
        return Ok(Some((name.to_string(), value)));
    }
    Ok(None)
}

fn stored_env_contains(env: &[(String, String)], name: &str) -> bool {
    env.iter().any(|(stored_name, _)| stored_name == name)
}

fn load_model_credentials_from_secret_store() -> Result<Vec<(String, String)>> {
    // Prefer an explicitly BYO AgentOS credential when saved, otherwise hydrate
    // the SDK credential names in the same order `select_passthrough_env` uses.
    if let Some(pair) = secret_store_env("AGENTOS_CREDENTIALS")? {
        return Ok(vec![pair]);
    }
    let mut env = Vec::new();
    if let Some(pair) = secret_store_env("CLAUDE_CODE_OAUTH_TOKEN")? {
        env.push(pair);
    }
    if let Some(pair) = secret_store_env("ANTHROPIC_API_KEY")? {
        env.push(pair);
    }
    Ok(env)
}

pub async fn start(opts: StartOpts) -> Result<()> {
    let plugin_dir = opts
        .plugin_dir
        .canonicalize()
        .with_context(|| format!("plugin dir not found: {}", opts.plugin_dir.display()))?;
    // Fail fast on a directory that is not a bundle; the runner would reject
    // it at boot anyway (real-model mode), with a worse error surface.
    let (plugin_name, manifest_version) = read_manifest(&plugin_dir)?;

    if opts.local_model.is_some() && opts.fake_model {
        return Err(crate::exit::usage(
            "--local-model cannot be combined with --fake-model",
        ));
    }
    if opts.local_model.is_some() && opts.model.is_some() {
        return Err(crate::exit::usage(
            "--local-model cannot be combined with --model",
        ));
    }

    if state::load(&plugin_dir)?.is_some() {
        return Err(crate::exit::usage(format!(
            "a local runner is already recorded in {}/.agentos/runner.json; run 'agentos skill down' there first",
            plugin_dir.display()
        )));
    }

    // Parse (not just forward) the budget so a typo fails here, not in-container.
    let _: Budget = serde_json::from_str(&opts.budget).map_err(|e| {
        crate::exit::usage(format!(
            "--budget is not a valid ACI budget: {}: {e}",
            opts.budget
        ))
    })?;

    let session_id = format!("local-{}", unix_now());
    let mut network = opts.network.clone();
    let mut owned_network: Option<String> = None;
    let mut ollama_container: Option<String> = None;
    let mut model_base_url: Option<String> = None;
    let mut model = opts.model.clone();

    if let Some(local_model) = &opts.local_model {
        let (net, owned) = match &opts.network {
            Some(net) => (net.clone(), false),
            None => (format!("{}-net", opts.name), true),
        };
        if owned {
            // Only claim ownership (and teardown responsibility) when this call
            // actually created the network; a pre-existing one is not ours to rm.
            let created = docker::create_network(&net).await?;
            if created {
                owned_network = Some(net.clone());
            }
        }
        let ollama = format!("{}-ollama", opts.name);
        if let Err(err) = docker::run_ollama(&ollama, &net, DEFAULT_OLLAMA_IMAGE).await {
            if let Some(net) = &owned_network {
                let _ = docker::remove_network(net).await;
            }
            return Err(err.context("starting local model container"));
        }
        if let Err(err) = docker::wait_ollama_ready(&ollama, Duration::from_secs(120)).await {
            let _ = docker::remove_container(&ollama).await;
            if let Some(net) = &owned_network {
                let _ = docker::remove_network(net).await;
            }
            return Err(err.context("waiting for local model container"));
        }
        if let Err(err) = docker::pull_model(&ollama, local_model).await {
            let _ = docker::remove_container(&ollama).await;
            if let Some(net) = &owned_network {
                let _ = docker::remove_network(net).await;
            }
            return Err(err.context("pulling local model"));
        }
        let url = format!("http://{ollama}:{OLLAMA_PORT}");
        network = Some(net);
        ollama_container = Some(ollama);
        model_base_url = Some(url);
        model = Some(local_model.clone());
    }

    // Forward exactly one model credential (or none under fake/local) -- never
    // the ambient SDK token alongside a chosen BYO credential. See
    // select_passthrough_env.
    let suppress_credential = opts.local_model.is_some() || opts.fake_model;
    let mut docker_env = Vec::new();
    if !suppress_credential {
        docker_env.extend(load_model_credentials_from_secret_store()?);
    }
    let byo_credential = std::env::var("AGENTOS_CREDENTIALS")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| {
            stored_env_contains(&docker_env, "AGENTOS_CREDENTIALS").then_some("stored".to_string())
        });
    // Hydrate `--secret NAME` from AgentOS private storage when it is not
    // already present in the process env. The docker argv still forwards only
    // the NAME (`-e NAME`); the value is supplied only to the Docker CLI child
    // process so Docker can copy it into the runner container.
    for name in &opts.secret {
        if !env_credential_present(name) && !stored_env_contains(&docker_env, name) {
            match secret_store_env(name)? {
                Some(pair) => docker_env.push(pair),
                None => {
                    crate::ui::ui().note(&format!(
                        "--secret {name}: not set in the environment or AgentOS secret store; nothing will be forwarded for it"
                    ));
                }
            }
        }
    }
    let passthrough_env = merge_secret_env(
        select_passthrough_env(suppress_credential, byo_credential.as_deref()),
        &opts.secret,
    );

    let spec = StartSpec {
        image: opts.image.clone(),
        container_name: opts.name.clone(),
        host_port: opts.port,
        plugin_dir: plugin_dir.clone(),
        session_id: session_id.clone(),
        sandbox_id: "local".into(),
        budget_json: opts.budget,
        fake_model: opts.local_model.is_none() && opts.fake_model,
        network,
        otel_endpoint: opts.otel_endpoint,
        model_base_url: model_base_url.clone(),
        model,
        passthrough_env,
        docker_env,
    };

    let ui = crate::ui::ui();
    ui.note(&format!(
        "starting runner container '{}' from '{}'",
        opts.name, opts.image
    ));
    let container_id = match docker::docker_with_env(&spec.run_args(), &spec.docker_env).await {
        Ok(id) => id,
        Err(err) => {
            if let Some(ollama) = &ollama_container {
                let _ = docker::remove_container(ollama).await;
            }
            if let Some(net) = &owned_network {
                let _ = docker::remove_network(net).await;
            }
            return Err(err.context("starting runner container"));
        }
    };

    let base_url = format!("http://localhost:{}", opts.port);
    let client = RunnerClient::new(&base_url)?;
    let cl = ui.checklist();
    let step = cl.step("waiting for runner");
    if let Err(err) = client.wait_healthy(Duration::from_secs(60)).await {
        step.fail("unhealthy");
        let logs = docker::container_logs(&opts.name, 40).await;
        ui.note(&logs);
        let _ = docker::remove_container(&opts.name).await;
        if let Some(ollama) = &ollama_container {
            let _ = docker::remove_container(ollama).await;
        }
        if let Some(net) = &owned_network {
            let _ = docker::remove_network(net).await;
        }
        ui.failure(&format!("runner failed to become healthy: {err}"));
        bail!("runner failed to become healthy: {err}");
    }
    step.done("healthy");

    // State lives with the bundle: init gitignores .agentos/ there, and the
    // follow-up commands are documented to run from the bundle directory. If
    // the save fails (e.g. a read-only bundle), tear the container down again:
    // a live runner with no recorded state would be invisible to stop/status.
    if let Err(err) = state::save(
        &plugin_dir,
        &RunnerState {
            container_id,
            container_name: opts.name.clone(),
            image: opts.image,
            port: opts.port,
            base_url: base_url.clone(),
            session_id,
            plugin_dir: plugin_dir.display().to_string(),
            fake_model: opts.fake_model,
            ollama_container: ollama_container.clone(),
            network: owned_network.clone(),
            model_base_url: model_base_url.clone(),
        },
    ) {
        let _ = docker::remove_container(&opts.name).await;
        if let Some(ollama) = &ollama_container {
            let _ = docker::remove_container(ollama).await;
        }
        if let Some(net) = &owned_network {
            let _ = docker::remove_network(net).await;
        }
        return Err(err.context("recording runner state (container removed again)"));
    }

    let version = git_short_sha(&plugin_dir)
        .await
        .map(|sha| format!("dev @ {sha}"))
        .unwrap_or_else(|| format!("{plugin_name} @ {manifest_version}"));
    let rows = [
        ("Local bot", base_url),
        (
            "Skill message",
            "agentos skill message \"<message>\"".to_string(),
        ),
        ("Skill eval", "agentos skill eval".to_string()),
        ("Version", version),
    ];
    ui.payload_plain(&boxed_summary("agentos dev environment", &rows));
    if let Some(local_model) = &opts.local_model {
        ui.note(&format!(
            "local model running in container '{}' from '{}' with model '{}'",
            ollama_container.as_deref().unwrap_or("unknown"),
            DEFAULT_OLLAMA_IMAGE,
            local_model
        ));
    }
    let cwd = Path::new(".").canonicalize()?;
    if cwd != plugin_dir {
        ui.note(&format!(
            "State recorded in {}/.agentos/runner.json; run skill down from that directory. skill message, skill eval, and skill status also work there. skill message and skill eval also accept --url.",
            plugin_dir.display()
        ));
    }
    Ok(())
}

pub async fn stop() -> Result<()> {
    let dir = Path::new(".");
    let ui = crate::ui::ui();
    let Some(saved) = state::load(dir)? else {
        bail!("no local runner recorded in .agentos/runner.json; run from the bundle directory");
    };
    match docker::remove_container(&saved.container_name).await {
        Ok(()) => ui.success(&format!(
            "stopped and removed container '{}'",
            saved.container_name
        )),
        // The container being gone already is a success for stop: clear the
        // state instead of wedging start/stop on a stale runner.json.
        Err(err) if err.to_string().contains("No such container") => {
            ui.note(&format!(
                "container '{}' was already gone; cleared stale state",
                saved.container_name
            ));
        }
        Err(err) => return Err(err),
    }
    if let Some(ollama) = &saved.ollama_container {
        match docker::remove_container(ollama).await {
            Ok(()) => ui.success(&format!("stopped and removed container '{ollama}'")),
            Err(err) if err.to_string().contains("No such container") => {
                ui.note(&format!("container '{ollama}' was already gone"));
            }
            Err(err) => ui.warn(&format!("could not remove container '{ollama}': {err}")),
        }
        // Keep the model-cache volume so the next `skill up` reuses the pulled
        // model instead of re-downloading it (mirrors compose `down` keeping
        // `ollama_data`). Removal is left to the user.
        let volume = docker::ollama_volume(ollama);
        ui.note(&format!(
            "kept model-cache volume '{volume}' for fast re-up; remove it with 'docker volume rm {volume}'"
        ));
    }
    if let Some(net) = &saved.network {
        match docker::remove_network(net).await {
            Ok(()) => ui.success(&format!("removed network '{net}'")),
            Err(err) if err.to_string().contains("No such network") => {
                ui.note(&format!("network '{net}' was already gone"));
            }
            Err(err) => ui.warn(&format!("could not remove network '{net}': {err}")),
        }
    }
    state::remove(dir)?;
    Ok(())
}

/// The `agentos skill status --json` payload: the runner base URL plus the
/// serialized session status. Generic over the status shape so it serves both
/// the frozen `SessionStatus` (contract test) and the runner's raw `/status`
/// body (the live call site), which are both left unconstrained by
/// `cli/schema/status.schema.json`. Pure so it stays contract-testable.
pub fn status_json<T: serde::Serialize>(url: &str, status: &T) -> serde_json::Value {
    serde_json::json!({ "url": url, "session": status })
}

/// The `agentos skill eval --json` payload: the pass/fail roll-up plus one row
/// per case. Pure so it stays unit/contract-testable against
/// `cli/schema/eval.schema.json`.
pub fn eval_json(
    results: &[(String, bool, f64)],
    passed: usize,
    total: usize,
) -> serde_json::Value {
    let cases: Vec<serde_json::Value> = results
        .iter()
        .map(|(id, ok, seconds)| serde_json::json!({ "id": id, "passed": ok, "seconds": seconds }))
        .collect();
    serde_json::json!({
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "cases": cases,
    })
}

pub async fn status(url: Option<String>) -> Result<()> {
    let url = resolve_url(url)?;
    let client = RunnerClient::new(&url)?;
    let status = client.status().await?;
    let ui = crate::ui::ui();
    if ui.json() {
        ui.emit_json(&status_json(&url, &status));
        return Ok(());
    }
    ui.note(&format!("runner {url}"));
    ui.payload_plain(&serde_json::to_string_pretty(&status)?);
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
    let ui = crate::ui::ui();
    let mut printer = TurnPrinter::default();

    // A "thinking" spinner marks the wait for the first token; it is cleared the
    // instant streaming begins (committing no line) so the agent answer streams
    // clean. `streamed` tracks whether any answer token reached stdout;
    // `at_line_start` tracks whether stdout is at a fresh line (no un-terminated
    // streamed text) so a stderr diagnostic never glues onto a token line.
    let cl = ui.checklist();
    let mut step = Some(cl.step("thinking"));
    let mut streamed = false;
    let mut at_line_start = true;

    let events = client
        .send_event(event_type, text, user, |event| {
            let part = printer.part_for(event);
            // Clear the "thinking" spinner on the FIRST rendered event of any
            // kind (token, note, or failure). A Note/Fail is written to stderr
            // immediately, so if one arrives before the first token it would
            // garble the still-live spinner line unless we drop it first.
            if matches!(
                part,
                Some(TurnPart::Token(_) | TurnPart::Note(_) | TurnPart::Fail(_))
            ) {
                if let Some(step) = step.take() {
                    step.clear();
                }
            }
            match part {
                // Answer tokens are raw payload -> stdout, concatenated at network
                // pace with no per-delta newline. Track mid-line state so a later
                // note closes an un-terminated line first.
                Some(TurnPart::Token(token)) => {
                    ui.answer(&token);
                    streamed = true;
                    at_line_start = token.ends_with('\n');
                }
                // Tool notes and errors are diagnostics -> stderr. If stdout is
                // mid-line, close that streamed line with a single newline first
                // so the note does not glue onto the token text. Under `-q` the
                // note itself is a no-op; the lone separating newline lands in
                // the middle of the streamed answer, which is just whitespace and
                // harmless to `| jq` (a median newline in the payload is fine).
                Some(TurnPart::Note(msg)) => {
                    if !at_line_start {
                        ui.print_tokens("\n");
                        at_line_start = true;
                    }
                    ui.note(&msg);
                }
                Some(TurnPart::Fail(msg)) => {
                    if !at_line_start {
                        ui.print_tokens("\n");
                        at_line_start = true;
                    }
                    ui.failure(&msg);
                }
                // The status trailer is emitted once at the end from events.last().
                Some(TurnPart::Status(_)) | None => {}
            }
        })
        .await?;

    // Nothing ever streamed (e.g. an empty final): drop the spinner silently.
    if let Some(step) = step.take() {
        step.clear();
    }

    if let Some(OutboundEvent::Final { status, .. }) = events.last() {
        // Close the streamed answer on stdout only if the last thing written was
        // un-terminated token text; if a note already added its own newline (or
        // the last token ended in one) skip it to avoid a blank line. The status
        // trailer is a diagnostic -> stderr.
        if streamed && !at_line_start {
            ui.print_tokens("\n");
        }
        ui.note(&format!("-- final ({})", status_str(status)));
        if *status == SessionStatus::ClassifiedFailure {
            std::process::exit(1);
        }
    }
    Ok(())
}

pub async fn eval(
    cases_path: Option<PathBuf>,
    url: Option<String>,
    models: Vec<String>,
    secrets: Vec<String>,
    image: String,
) -> Result<()> {
    let state_plugin_dir = state::load(Path::new("."))?.map(|s| PathBuf::from(s.plugin_dir));
    let cases_path = resolve_cases_path(cases_path, Path::new("."), state_plugin_dir.as_deref())?;
    let suite = load_suite(&cases_path)?;

    // Model selection (#526): with `--model`, boot a transient runner per model,
    // run the suite against each, and report pass-rate per model -- the one
    // command a "can we move to a cheaper model" decision needs, instead of a
    // manual `skill up --model X` + `skill eval` loop per model. Without it, the
    // default path drives the already-running runner (whatever model it booted).
    if !models.is_empty() {
        return eval_sweep(
            &suite,
            &models,
            &secrets,
            &image,
            state_plugin_dir.as_deref(),
        )
        .await;
    }

    let url = resolve_url(url)?;
    let client = RunnerClient::new(&url)?;
    let ui = crate::ui::ui();
    let bar = ui.progress_bar(suite.cases.len() as u64, "running evals");
    let results = run_suite_cases(&client, &suite, |_| bar.inc(1)).await?;
    bar.finish();

    report_eval(&results)
}

/// Run every case in `suite` against a runner, returning `(id, passed, seconds)`
/// rows. `on_case` is called once per completed case (progress). Shared by the
/// single-runner path and the per-model sweep so both grade identically.
async fn run_suite_cases(
    client: &RunnerClient,
    suite: &EvalSuite,
    mut on_case: impl FnMut(usize),
) -> Result<Vec<(String, bool, f64)>> {
    let mut results = Vec::with_capacity(suite.cases.len());
    for (i, case) in suite.cases.iter().enumerate() {
        let started = Instant::now();
        let events = client
            .send_event(EventType::EvalCase, &case.input, "U-eval", |_| {})
            .await?;
        let elapsed = started.elapsed().as_secs_f64();
        results.push((case.id.clone(), turn_passes(case, &events), elapsed));
        on_case(i);
    }
    Ok(results)
}

/// Boot a throwaway runner for one model on `port`, forwarding the model
/// credential and any `--secret` from the env or the host vault exactly like
/// `skill up` (never in argv). Returns its base URL; the caller removes the
/// container when done. Does NOT touch `.agentos/runner.json`, so a sweep never
/// clobbers a persistent `skill up` runner's recorded state.
async fn boot_eval_runner(
    plugin_dir: &Path,
    image: &str,
    port: u16,
    name: &str,
    model: &str,
    secrets: &[String],
) -> Result<String> {
    // Real-model run: forward the model credential (env or vault) and the
    // bundle's --secret connector secrets, mirroring `start`'s resolution.
    let mut docker_env = load_model_credentials_from_secret_store()?;
    let byo_credential = std::env::var("AGENTOS_CREDENTIALS").ok().or_else(|| {
        stored_env_contains(&docker_env, "AGENTOS_CREDENTIALS").then_some("stored".to_string())
    });
    for secret in secrets {
        if std::env::var_os(secret).is_none() && !stored_env_contains(&docker_env, secret) {
            if let Some(pair) = secret_store_env(secret)? {
                docker_env.push(pair);
            }
        }
    }
    let passthrough_env = merge_secret_env(
        select_passthrough_env(false, byo_credential.as_deref()),
        secrets,
    );
    let spec = StartSpec {
        image: image.to_string(),
        container_name: name.to_string(),
        host_port: port,
        plugin_dir: plugin_dir.to_path_buf(),
        session_id: format!("eval-{}", unix_now()),
        sandbox_id: "local".into(),
        budget_json: DEFAULT_BUDGET.to_string(),
        fake_model: false,
        network: None,
        otel_endpoint: None,
        model_base_url: None,
        model: Some(model.to_string()),
        passthrough_env,
        docker_env,
    };
    docker::docker_with_env(&spec.run_args(), &spec.docker_env)
        .await
        .with_context(|| format!("booting eval runner for model {model}"))?;
    let url = format!("http://localhost:{port}");
    if let Err(err) = RunnerClient::new(&url)?
        .wait_healthy(Duration::from_secs(60))
        .await
    {
        let logs = docker::container_logs(name, 40).await;
        let _ = docker::remove_container(name).await;
        bail!("eval runner for model {model} failed to become healthy: {err}\n{logs}");
    }
    Ok(url)
}

/// Run the suite once per model in a fresh runner and report pass-rate per model.
async fn eval_sweep(
    suite: &EvalSuite,
    models: &[String],
    secrets: &[String],
    image: &str,
    state_plugin_dir: Option<&Path>,
) -> Result<()> {
    let ui = crate::ui::ui();
    // Mount the recorded runner's bundle dir if one is known, else the cwd.
    let plugin_dir = state_plugin_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
        .canonicalize()
        .context("resolving the bundle directory for the model sweep")?;
    ui.note(&format!(
        "model sweep: {} model(s) x {} case(s)",
        models.len(),
        suite.cases.len()
    ));
    let cl = ui.checklist();
    let mut rows: Vec<(String, usize, usize)> = Vec::with_capacity(models.len());
    for (i, model) in models.iter().enumerate() {
        let name = format!("agentos-eval-sweep-{i}");
        let port = DEFAULT_PORT + 100 + i as u16;
        let step = cl.step(&format!("model {model}"));
        let url = match boot_eval_runner(&plugin_dir, image, port, &name, model, secrets).await {
            Ok(url) => url,
            Err(err) => {
                step.fail("boot failed");
                return Err(err);
            }
        };
        let client = RunnerClient::new(&url)?;
        let run = run_suite_cases(&client, suite, |_| {}).await;
        let _ = docker::remove_container(&name).await;
        let results = run?;
        let passed = results.iter().filter(|(_, ok, _)| *ok).count();
        step.done(&format!("{passed}/{}", suite.cases.len()));
        rows.push((model.clone(), passed, suite.cases.len()));
    }
    report_sweep(&rows)
}

/// Render a model-sweep roll-up: pass-rate per model. Under `--json` the whole
/// comparison is one payload; otherwise a table. A sweep is a comparison, not a
/// gate, so it never exits non-zero on a model that scored below 100%.
pub fn report_sweep(rows: &[(String, usize, usize)]) -> Result<()> {
    let ui = crate::ui::ui();
    if ui.json() {
        let models: Vec<serde_json::Value> = rows
            .iter()
            .map(|(model, passed, total)| {
                serde_json::json!({
                    "model": model,
                    "passed": passed,
                    "total": total,
                    "pass_rate": if *total > 0 { *passed as f64 / *total as f64 } else { 0.0 },
                })
            })
            .collect();
        ui.emit_json(&serde_json::json!({ "sweep": models }));
        return Ok(());
    }
    let table: Vec<Vec<String>> = rows
        .iter()
        .map(|(model, passed, total)| {
            let rate = if *total > 0 {
                *passed as f64 / *total as f64 * 100.0
            } else {
                0.0
            };
            vec![
                model.clone(),
                format!("{passed}/{total}"),
                format!("{rate:.0}%"),
            ]
        })
        .collect();
    ui.payload_plain(&crate::ui::table(
        &["model", "passed", "pass rate"],
        &table,
        &[1, 2],
    ));
    Ok(())
}

/// Render a finished eval run identically for every tier (`skill`, `local`,
/// `cluster`): under `--json` the whole roll-up is one machine payload on
/// stdout; otherwise the per-case table is payload -> stdout and the roll-up
/// verdict is a diagnostic -> stderr. A failing run exits `Failure`. Shared so
/// `local eval`/`cluster eval` print the same summary `skill eval` does (the
/// per-tier parity gate), not a hand-mirrored one.
pub fn report_eval(results: &[(String, bool, f64)]) -> Result<()> {
    let ui = crate::ui::ui();
    let total = results.len();
    let passed = results.iter().filter(|(_, ok, _)| *ok).count();

    if ui.json() {
        ui.emit_json(&eval_json(results, passed, total));
        if passed < total {
            std::process::exit(crate::exit::ExitClass::Failure.code());
        }
        return Ok(());
    }

    let rows: Vec<Vec<String>> = results
        .iter()
        .map(|(name, ok, seconds)| {
            let result = if *ok {
                format!("{} pass", '\u{2713}')
            } else {
                format!("{} fail", '\u{2717}')
            };
            vec![name.clone(), result, format!("{seconds:.1}s")]
        })
        .collect();
    ui.payload_plain(&crate::ui::table(&["case", "result", "time"], &rows, &[2]));
    if passed == total {
        ui.success(&format!("{passed}/{total} passed"));
    } else {
        ui.warn(&format!(
            "{passed}/{total} passed; {} failed",
            total - passed
        ));
    }
    if passed < total {
        std::process::exit(crate::exit::ExitClass::Failure.code());
    }
    Ok(())
}

/// Where the eval cases live: an explicit `--cases` wins; otherwise
/// `evals/cases.json` in the current directory, falling back to the started
/// runner's recorded bundle directory (so `agentos skill eval` works from
/// wherever `agentos skill up` was run).
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
    Err(crate::exit::CliError::usage(format!(
        "no eval cases found: looked for {} and the running bundle's evals/cases.json; pass --cases",
        local.display()
    ))
    .with_fix("pass --cases")
    .into())
}

pub struct DeployOpts {
    pub plugin_dir: PathBuf,
    pub api_url: String,
    pub api_key: String,
    /// Explicit `--slack-channel`; None when the flag was omitted so a redeploy
    /// leaves an existing agent's channel untouched instead of masking intent
    /// with a default.
    pub slack_channel: Option<String>,
    pub env: DeployEnv,
    pub label: Option<String>,
    /// Per-agent connector secret NAMES to bind on deploy (ADR-0009, #429). Each
    /// value is resolved from the caller's env or the host secret vault and sent
    /// to the platform API, which stores it on the agent for the worker to
    /// forward into the sandbox. From `deploy --secret <NAME>`.
    pub secret: Vec<String>,
    /// Whether this tier offers `--secret` binding, gating the declared-secrets
    /// policy check (#464): true for tiers that can bind a declared secret
    /// (local), false where secret delivery is not yet wired (cluster, until
    /// #440 flips it). When false the gate is skipped -- otherwise every
    /// secrets-declaring bundle would hard-fail with a `--secret <NAME>`
    /// remediation that does not exist on that tier.
    pub secret_binding_supported: bool,
    /// Actionable remediation line printed when the platform API connection
    /// fails (e.g. the kubectl port-forward command for cluster, or
    /// `agentos local up` for local). Naming the fix turns a raw
    /// "Connection refused" into something the operator can act on.
    pub connect_hint: String,
}

/// The declared connector-secret NAMES not present in the operator's bound
/// `--secret` set (#464). A non-empty result is a deploy-time gap: the bundle
/// expects a secret nothing will bind, which would surface at runtime as an
/// auth failure (#429).
///
/// Only WELL-FORMED, bindable names count as a gap: a declared name is diffed
/// only when it passes `crate::secrets::validate_name`, the same env-var-syntax
/// check (`^[A-Z_][A-Z0-9_]*$`) used for `agentos secrets set`. Malformed or
/// reserved names are the plugin-format validator's responsibility (server-side
/// on upload); the gate excludes them so it never preempts that real validation
/// error with a misleading "bind `--secret <NAME>`" message. The reserved-name
/// list is deliberately NOT mirrored into Rust (drift risk) -- the regex filter
/// is the intended scope.
fn unbound_declared_secrets(declared: &[String], bound: &[String]) -> Vec<String> {
    declared
        .iter()
        .filter(|name| {
            crate::secrets::validate_name(name).is_ok() && !bound.iter().any(|b| b == *name)
        })
        .cloned()
        .collect()
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

    // Deploy-time secrets-policy gate (#464 / ADR-0009): every NAME the bundle's
    // manifest `secrets` policy declares must be in the operator's bound
    // `--secret` set, else deploy FAILS naming the gap. Decision is fail-loud per
    // the ticket -- a missing binding otherwise surfaces later as a runtime auth
    // failure (#429). This runs in the shared deploy() path, pre-network, so it
    // covers BOTH `local deploy` and `cluster deploy`. It is gated on
    // `secret_binding_supported` (AC2): cluster deploy cannot bind a `--secret`
    // until #440 wires delivery, so enforcing there would hard-fail every
    // secrets-declaring bundle with a remediation the tier cannot satisfy. It
    // runs first, before the archive is even packed: the check is a pure
    // name-set diff on `opts.secret` (the bound NAME set) and needs no packed
    // bundle or resolved values, so a declared-but-unbound policy fails fast
    // without doing any of that work.
    if opts.secret_binding_supported {
        let declared = read_declared_secrets(&plugin_dir)?;
        let unbound = unbound_declared_secrets(&declared, &opts.secret);
        if !unbound.is_empty() {
            return Err(crate::exit::usage(format!(
                "{plugin_name} declares connector secret(s) that were not bound on deploy: {}. \
                 Bind each with `--secret <NAME>` (value read from the environment or from \
                 `agentos secrets set <NAME>`).",
                unbound.join(", ")
            )));
        }
    }

    let ui = crate::ui::ui();
    if let Some(channel) = opts.slack_channel.as_deref() {
        validate_slack_channel(channel)?;
    }
    let archive = pack_tar_gz(&plugin_dir)?;
    let env = opts.env.as_str();
    ui.note(&format!(
        "deploying {plugin_name} ({} bytes) to {} [{env}]",
        archive.len(),
        opts.api_url,
    ));

    // Resolve each --secret NAME to a value (env wins, else the host vault) so
    // the connector secret is bound on the agent for the worker to forward into
    // the sandbox (ADR-0009, #429). The value never appears in argv.
    let mut secrets: std::collections::BTreeMap<String, String> = std::collections::BTreeMap::new();
    for name in &opts.secret {
        let value = std::env::var(name)
            .ok()
            .filter(|v| !v.is_empty())
            .or(crate::secrets::get_value(name)?);
        match value {
            Some(v) => {
                secrets.insert(name.clone(), v);
            }
            None => {
                return Err(crate::exit::usage(format!(
                    "--secret {name}: not set in the environment and not saved in AgentOS \
                     storage; export it or run `agentos secrets set {name}` first"
                )));
            }
        }
    }
    if !secrets.is_empty() {
        ui.note(&format!(
            "binding {} connector secret(s): {}",
            secrets.len(),
            secrets.keys().cloned().collect::<Vec<_>>().join(", ")
        ));
    }

    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let cl = ui.checklist();
    let step = cl.step(&format!("deploying {plugin_name}"));
    let outcome = match client
        .deploy(
            &plugin_name,
            opts.slack_channel.as_deref(),
            &label,
            &created_by,
            env,
            archive,
            &secrets,
        )
        .await
    {
        Ok(outcome) => {
            step.done(env);
            outcome
        }
        Err(err) => {
            step.fail("failed");
            if crate::exit::is_transient_reqwest(&err) {
                return Err(err.context(opts.connect_hint));
            }
            return Err(err);
        }
    };

    ui.payload(&format!("deployed {plugin_name} {label} -> {env}"));
    ui.kv(
        "agent",
        &format!("{} ({})", outcome.agent.name, ui.url(&outcome.agent.id)),
    );
    ui.kv(
        "version",
        &format!(
            "{} ({})",
            outcome.version.version_label,
            ui.url(&outcome.version.id)
        ),
    );
    let channel = match &outcome.channel {
        ChannelOutcome::Created(channel) => channel.clone(),
        ChannelOutcome::Updated { from, to } => format!("updated to {to} (was {from})"),
        ChannelOutcome::Unchanged { channel, passed } => {
            if *passed {
                format!("unchanged ({channel})")
            } else {
                format!("unchanged ({channel}); pass --slack-channel to move it")
            }
        }
    };
    ui.kv("channel", &channel);
    ui.kv(
        "bundle",
        &format!(
            "{} sha256:{} {} bytes",
            outcome.bundle.bundle_ref, outcome.bundle.bundle_sha256, outcome.bundle.size_bytes
        ),
    );
    ui.kv(
        "deployment",
        &format!(
            "{} [{}] {}",
            outcome.deployment.id, outcome.deployment.environment, outcome.deployment.status
        ),
    );
    Ok(())
}

/// Shared flags for the agent-lifecycle verbs (`cluster kill|resume|budget|delete`).
/// Like `deploy`, these speak the committed platform-API contract through the
/// existing `ApiClient` (no second HTTP client).
pub struct AgentActionOpts {
    pub api_url: String,
    pub api_key: String,
    /// Agent name or id to act on. Resolved to the API's `{agent_id}` via the
    /// same name lookup `deploy` uses (`ApiClient::find_agent`).
    pub agent: String,
    pub dry_run: bool,
}

/// Output of `<tier> kill <agent>`: the dry-run plan, or the resulting kill
/// state. Owns its data (agent name) so `to_json` / `render` outlive the
/// `ApiClient`. The json-vs-human choice is made once, in `Ui::emit` (#456).
#[derive(Debug)]
pub enum KillOutput {
    DryRun(crate::ui::DryRunPlan),
    Done { agent: String, killed: bool },
}

impl crate::ui::CliOutput for KillOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            KillOutput::DryRun(plan) => plan.to_json(),
            KillOutput::Done { agent, killed } => {
                serde_json::json!({"agent": agent, "killed": killed})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            KillOutput::DryRun(plan) => plan.render(ui),
            KillOutput::Done { agent, killed } => {
                ui.payload(&format!("agent {agent} killed (killed={killed})"));
                ui.note("Run `agentos cluster resume <agent>` to bring it back.");
            }
        }
    }
}

/// `agentos cluster kill <agent> --yes`: flip the agent kill switch on
/// (`POST /agents/{id}/kill`). Destructive (it stops the agent's runs), so it
/// refuses without `--yes`, mirroring `cluster down`. `--dry-run` returns the
/// plan and makes no request.
pub async fn kill(opts: AgentActionOpts, yes: bool) -> Result<KillOutput> {
    let ui = crate::ui::ui();
    if opts.dry_run {
        return Ok(KillOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "POST {}/agents/<id>/kill  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    if !yes {
        return Err(crate::exit::CliError::usage(format!(
            "`agentos cluster kill {}` stops the agent's runs; re-run with --yes to confirm",
            opts.agent
        ))
        .with_fix("re-run with --yes")
        .into());
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let cl = ui.checklist();
    let step = cl.step(&format!("killing {}", agent.name));
    let state = match client.kill_agent(&agent.id).await {
        Ok(state) => {
            step.done("killed");
            state
        }
        Err(err) => {
            step.fail("failed");
            return Err(err);
        }
    };
    Ok(KillOutput::Done {
        agent: agent.name,
        killed: state.killed,
    })
}

/// Output of `<tier> resume <agent>`: the dry-run plan, or the resulting kill
/// state. Owns its data so it outlives the `ApiClient`.
#[derive(Debug)]
pub enum ResumeOutput {
    DryRun(crate::ui::DryRunPlan),
    Done { agent: String, killed: bool },
}

impl crate::ui::CliOutput for ResumeOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ResumeOutput::DryRun(plan) => plan.to_json(),
            ResumeOutput::Done { agent, killed } => {
                serde_json::json!({"agent": agent, "killed": killed})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ResumeOutput::DryRun(plan) => plan.render(ui),
            ResumeOutput::Done { agent, killed } => {
                ui.payload(&format!("agent {agent} resumed (killed={killed})"));
            }
        }
    }
}

/// `agentos cluster resume <agent>`: flip the agent kill switch off
/// (`POST /agents/{id}/resume`). Non-destructive, so no `--yes` gate.
/// `--dry-run` returns the plan and makes no request.
pub async fn resume(opts: AgentActionOpts) -> Result<ResumeOutput> {
    let ui = crate::ui::ui();
    if opts.dry_run {
        return Ok(ResumeOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "POST {}/agents/<id>/resume  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let cl = ui.checklist();
    let step = cl.step(&format!("resuming {}", agent.name));
    let state = match client.resume_agent(&agent.id).await {
        Ok(state) => {
            step.done("resumed");
            state
        }
        Err(err) => {
            step.fail("failed");
            return Err(err);
        }
    };
    Ok(ResumeOutput::Done {
        agent: agent.name,
        killed: state.killed,
    })
}

/// Output of `<tier> budget <agent>`: the dry-run plan, or the saved budget.
/// `max_usd_per_day` is `None` when the platform default applies. Owns its data
/// so it outlives the `ApiClient`.
#[derive(Debug)]
pub enum BudgetOutput {
    DryRun(crate::ui::DryRunPlan),
    Done {
        agent: String,
        max_usd_per_day: Option<f64>,
    },
}

impl crate::ui::CliOutput for BudgetOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            BudgetOutput::DryRun(plan) => plan.to_json(),
            BudgetOutput::Done {
                agent,
                max_usd_per_day,
            } => serde_json::json!({"agent": agent, "max_usd_per_day": max_usd_per_day}),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            BudgetOutput::DryRun(plan) => plan.render(ui),
            BudgetOutput::Done {
                agent,
                max_usd_per_day,
            } => {
                let usd = max_usd_per_day
                    .map(|v| format!("${v}/day"))
                    .unwrap_or_else(|| "platform default".to_string());
                ui.payload(&format!("budget for {agent} set: max $/day {usd}"));
            }
        }
    }
}

/// `agentos cluster budget <agent> --limit <n>`: set the agent budget
/// (`PUT /agents/{id}/budget`). `--limit` sets the daily spend cap
/// (`max_usd_per_day`, the primary `BudgetConfig` field the console surfaces as
/// "Max $/day"); the per-run token cap is left at the platform default.
/// `--dry-run` returns the plan and makes no request.
pub async fn budget(opts: AgentActionOpts, limit: f64) -> Result<BudgetOutput> {
    let ui = crate::ui::ui();
    if opts.dry_run {
        return Ok(BudgetOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "PUT {}/agents/<id>/budget  {{\"max_usd_per_day\":{limit}}}  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    if !limit.is_finite() || limit <= 0.0 {
        return Err(crate::exit::usage(format!(
            "--limit must be a finite value greater than 0 (got {limit})"
        )));
    }
    let cfg = BudgetConfig {
        max_output_tokens_per_run: None,
        max_usd_per_day: Some(limit),
    };
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let cl = ui.checklist();
    let step = cl.step(&format!("setting budget for {}", agent.name));
    let saved = match client.set_budget(&agent.id, &cfg).await {
        Ok(saved) => {
            step.done("updated");
            saved
        }
        Err(err) => {
            step.fail("failed");
            return Err(err);
        }
    };
    Ok(BudgetOutput::Done {
        agent: agent.name,
        max_usd_per_day: saved.max_usd_per_day,
    })
}

/// Output of `<tier> delete <agent>`: the dry-run plan, or the deleted agent's
/// name. Owns its data so it outlives the `ApiClient`.
#[derive(Debug)]
pub enum DeleteOutput {
    DryRun(crate::ui::DryRunPlan),
    Done { agent: String },
}

impl crate::ui::CliOutput for DeleteOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            DeleteOutput::DryRun(plan) => plan.to_json(),
            DeleteOutput::Done { agent } => serde_json::json!({"agent": agent, "deleted": true}),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            DeleteOutput::DryRun(plan) => plan.render(ui),
            DeleteOutput::Done { agent } => ui.payload(&format!("agent {agent} deleted")),
        }
    }
}

/// `agentos cluster delete <agent> --yes`: delete the agent
/// (`DELETE /agents/{id}`). Destructive and irreversible, so it refuses without
/// `--yes`, mirroring `cluster down`. `--dry-run` returns the plan and makes no
/// request.
pub async fn delete(opts: AgentActionOpts, yes: bool) -> Result<DeleteOutput> {
    let ui = crate::ui::ui();
    if opts.dry_run {
        return Ok(DeleteOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "DELETE {}/agents/<id>  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    if !yes {
        return Err(crate::exit::CliError::usage(format!(
            "`agentos cluster delete {}` permanently deletes the agent; re-run with --yes to confirm",
            opts.agent
        ))
        .with_fix("re-run with --yes")
        .into());
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let cl = ui.checklist();
    let step = cl.step(&format!("deleting {}", agent.name));
    match client.delete_agent(&agent.id).await {
        Ok(()) => step.done("deleted"),
        Err(err) => {
            step.fail("failed");
            return Err(err);
        }
    }
    Ok(DeleteOutput::Done { agent: agent.name })
}

/// Output of `<tier> versions <agent>`: the dry-run plan, the empty case, or the
/// version list. Owns its data (agent name + cloned versions) so `to_json` /
/// `render` outlive the `ApiClient`. The json-vs-human choice is made once, in
/// `Ui::emit` (issue #456).
pub enum VersionsOutput {
    DryRun(crate::ui::DryRunPlan),
    Empty {
        agent: String,
    },
    /// `versions` is held **newest-first**, normalized once by the `versions`
    /// handler (the API returns them oldest-first). Both `to_json` and `render`
    /// iterate it plainly, so any future constructor must preserve that order or
    /// the two paths silently diverge.
    List {
        agent: String,
        versions: Vec<crate::api::Version>,
    },
}

impl crate::ui::CliOutput for VersionsOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            VersionsOutput::DryRun(plan) => plan.to_json(),
            VersionsOutput::Empty { agent } => {
                serde_json::json!({"agent": agent, "versions": []})
            }
            VersionsOutput::List { agent, versions } => {
                let versions: Vec<serde_json::Value> = versions
                    .iter()
                    .map(|v| {
                        serde_json::json!({
                            "version_label": v.version_label,
                            "commit_sha": v.commit_sha,
                            "created_by": v.created_by,
                            "created_at": v.created_at,
                        })
                    })
                    .collect();
                serde_json::json!({"agent": agent, "versions": versions})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            VersionsOutput::DryRun(plan) => plan.render(ui),
            VersionsOutput::Empty { agent } => {
                ui.payload(&format!("{agent} has no versions yet (deploy it first)"));
            }
            VersionsOutput::List { agent, versions } => {
                ui.payload(&format!(
                    "{agent} — {} version(s), newest first:",
                    versions.len()
                ));
                for v in versions.iter() {
                    let commit = v.commit_sha.as_deref().unwrap_or("-");
                    let by = v.created_by.as_deref().unwrap_or("-");
                    let at = v.created_at.as_deref().unwrap_or("-");
                    ui.kv(
                        &v.version_label,
                        &format!("commit {commit}  by {by}  at {at}"),
                    );
                }
            }
        }
    }
}

/// `<tier> versions <agent>`: list the agent's immutable versions (newest first).
pub async fn versions(opts: AgentActionOpts) -> Result<VersionsOutput> {
    if opts.dry_run {
        return Ok(VersionsOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "GET {}/agents/<id>/versions  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let versions = client.list_versions(&agent.id).await?;
    if versions.is_empty() {
        return Ok(VersionsOutput::Empty { agent: agent.name });
    }
    // The API returns versions oldest-first; normalize to the documented
    // newest-first order HERE, once, so the json and human paths cannot diverge.
    Ok(VersionsOutput::List {
        agent: agent.name,
        versions: versions.into_iter().rev().collect(),
    })
}

/// Output of `<tier> memory <agent>`: the dry-run plan, the empty case, or the
/// learned-memory list. Owns its data so it outlives the `ApiClient`.
pub enum MemoryOutput {
    DryRun(crate::ui::DryRunPlan),
    Empty {
        agent: String,
    },
    List {
        agent: String,
        entries: Vec<crate::api::MemoryEntry>,
    },
}

impl crate::ui::CliOutput for MemoryOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            MemoryOutput::DryRun(plan) => plan.to_json(),
            MemoryOutput::Empty { agent } => {
                serde_json::json!({"agent": agent, "entries": []})
            }
            MemoryOutput::List { agent, entries } => {
                let entries: Vec<serde_json::Value> = entries
                    .iter()
                    .map(|e| serde_json::json!({"index": e.index, "content": e.content}))
                    .collect();
                serde_json::json!({"agent": agent, "entries": entries})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            MemoryOutput::DryRun(plan) => plan.render(ui),
            MemoryOutput::Empty { agent } => {
                ui.payload(&format!("{agent} has no learned memory yet"));
            }
            MemoryOutput::List { agent, entries } => {
                ui.payload(&format!("{agent} — {} memory entr(ies):", entries.len()));
                for e in entries {
                    ui.kv(&format!("#{}", e.index), &e.content);
                }
            }
        }
    }
}

/// `<tier> memory <agent>`: show what the agent has learned (its memory log).
pub async fn memory(opts: AgentActionOpts) -> Result<MemoryOutput> {
    if opts.dry_run {
        return Ok(MemoryOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "GET {}/agents/<id>/memory  (would resolve agent {:?} first)",
                opts.api_url, opts.agent
            )],
        }));
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let entries = client.list_memory(&agent.id).await?;
    if entries.is_empty() {
        return Ok(MemoryOutput::Empty { agent: agent.name });
    }
    Ok(MemoryOutput::List {
        agent: agent.name,
        entries,
    })
}

/// Output of `<tier> approvals <agent>`: the dry-run plan, or the gate list
/// (empty vec == "no tools gated"). Owns its data so it outlives the `ApiClient`.
pub enum ApprovalsOutput {
    DryRun(crate::ui::DryRunPlan),
    Gates {
        agent: String,
        gated_tools: Vec<String>,
    },
}

impl crate::ui::CliOutput for ApprovalsOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ApprovalsOutput::DryRun(plan) => plan.to_json(),
            ApprovalsOutput::Gates { agent, gated_tools } => {
                serde_json::json!({"agent": agent, "gated_tools": gated_tools})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ApprovalsOutput::DryRun(plan) => plan.render(ui),
            ApprovalsOutput::Gates { agent, gated_tools } => {
                if gated_tools.is_empty() {
                    ui.payload(&format!(
                        "{agent}: no tools are gated (calls run without approval)"
                    ));
                } else {
                    ui.payload(&format!("{agent} — {} gated tool(s):", gated_tools.len()));
                    for tool in gated_tools {
                        ui.kv("gated", tool);
                    }
                }
            }
        }
    }
}

/// `<tier> approvals <agent> [--gate TOOL]... [--clear]`: view or set the tool
/// names whose calls pause for human approval. No flags => show current gates.
pub async fn approvals(
    opts: AgentActionOpts,
    gate: Vec<String>,
    clear: bool,
) -> Result<ApprovalsOutput> {
    if clear && !gate.is_empty() {
        return Err(crate::exit::usage(
            "--clear cannot be combined with --gate (clear removes all gates)",
        ));
    }
    let setting = clear || !gate.is_empty();
    if opts.dry_run {
        let action = if setting {
            format!(
                "PATCH {}/agents/<id> approval_required_tools={:?}",
                opts.api_url, gate
            )
        } else {
            format!("GET {}/agents/<id> (show current gates)", opts.api_url)
        };
        return Ok(ApprovalsOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!(
                "{action}  (would resolve agent {:?} first)",
                opts.agent
            )],
        }));
    }
    let ui = crate::ui::ui();
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let agent = client.find_agent(&opts.agent).await?;
    let gates = if setting {
        let cl = ui.checklist();
        let step = cl.step(&format!("updating approval gates for {}", agent.name));
        match client.set_approval_tools(&agent.id, &gate).await {
            Ok(updated) => {
                step.done("updated");
                updated.approval_required_tools.unwrap_or_default()
            }
            Err(err) => {
                step.fail("failed");
                return Err(err);
            }
        }
    } else {
        agent.approval_required_tools.clone().unwrap_or_default()
    };
    Ok(ApprovalsOutput::Gates {
        agent: agent.name,
        gated_tools: gates,
    })
}

/// `local observability`: print the local platform's observability surfaces --
/// the AgentOS Console, the Langfuse UI, and the API base -- resolved through the
/// shared tier-aware endpoint seam (`crate::observability`).
///
/// `ObservabilityOutput` moved to `crate::observability` (#460) so both tiers
/// return one type; the hardcoded URL array that used to live here is replaced
/// by `observability::local_endpoints()`, whose consts `local.rs::ENDPOINTS`
/// also references (one source of truth for the port literals).
///
/// Agent-first: a browser is opened only when the human passes `--open`, and
/// never under `--json` (gated by `observability::should_open`). A missing
/// opener (headless/CI) is not an error -- the URLs are printed either way.
pub async fn observability(open: bool) -> Result<crate::observability::ObservabilityOutput> {
    let ui = crate::ui::ui();
    let surfaces = crate::observability::local_endpoints();
    crate::observability::open_endpoints(&surfaces, open, ui.json()).await;
    // A hint, not payload: `observability` never checks whether the stack is
    // up, so this is stderr guidance rather than a claim about what happened.
    ui.note("start these surfaces with `agentos local up` if they are unreachable");
    Ok(crate::observability::ObservabilityOutput::Surfaces(
        surfaces,
    ))
}

/// Reject a Slack channel value that is a `#name` rather than a channel ID.
///
/// Real Slack events carry the channel **ID** (e.g. `C0123ABCD`), and the
/// worker's binding resolver matches on that ID, so a `#name` value is stored
/// verbatim and never routes -- a silently dead binding. Fail the deploy up
/// front instead.
fn validate_slack_channel(channel: &str) -> Result<()> {
    if channel.trim_start().starts_with('#') {
        return Err(crate::exit::usage(format!(
            "slack channel {channel:?} is a name, not an ID: real Slack events carry the \
             channel ID (e.g. C0123ABCD) and the worker routes on it, so a #name binding \
             never receives messages. Pass the channel ID instead -- find it in the \
             channel's About tab, or the channel URL (.../archives/C0123ABCD)."
        )));
    }
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
    use super::{
        merge_secret_env, resolve_cases_path, seed_env_if_missing, select_passthrough_env,
        validate_slack_channel, EnvSeed,
    };
    use std::path::{Path, PathBuf};

    /// Scaffold a bundle at `dir` under `name`, then overwrite its manifest's
    /// `secrets` policy with `secrets`. Shared setup for tests exercising the
    /// declared-secrets gate in `deploy()`.
    fn scaffold_with_secrets(dir: &Path, name: &str, secrets: &[&str]) {
        crate::scaffold::scaffold(dir, name).unwrap();
        let manifest_path = dir.join(".claude-plugin/plugin.json");
        let mut manifest: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
        manifest["secrets"] = serde_json::json!(secrets);
        std::fs::write(
            &manifest_path,
            serde_json::to_string_pretty(&manifest).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn default_channel_passes_local_validation() {
        assert!(validate_slack_channel(crate::api::DEFAULT_SLACK_CHANNEL).is_ok());
    }

    #[test]
    fn parse_check_report_accepts_declared_authed_flag() {
        // The frozen check-report contract gains `authed` on each declared server
        // so the CLI/UI can flag credential-gated servers the offline check never
        // exercised. It must round-trip through parse_check_report.
        let json = r#"{
            "check": "mcp-load",
            "version": 1,
            "plugin_dir": "/x",
            "declared": [
                {"name": "github", "source": ".mcp.json", "form": "bare_file", "authed": true}
            ],
            "registered": [],
            "matches": [],
            "verdict": "green",
            "reasons": [],
            "hints": []
        }"#;
        let report = super::parse_check_report(json).expect("authed report must parse");
        assert!(
            report.declared[0].authed,
            "declared[].authed must round-trip true"
        );
    }

    #[test]
    fn parse_check_report_defaults_authed_false_when_absent() {
        // Backward compat: a report from an older runner has no `authed` key. It
        // must still parse and default to false (#[serde(default)]), never fail
        // the contract on the missing field.
        let json = r#"{
            "check": "mcp-load",
            "version": 1,
            "plugin_dir": "/x",
            "declared": [
                {"name": "plain", "source": "plugin.json", "form": "inline"}
            ],
            "registered": [],
            "matches": [],
            "verdict": "green",
            "reasons": [],
            "hints": []
        }"#;
        let report = super::parse_check_report(json).expect("report without authed must parse");
        assert!(
            !report.declared[0].authed,
            "absent authed must default to false"
        );
    }

    #[test]
    fn install_preserves_existing_local_config() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join(".env"), "USER_SETTING=keep-me\n").unwrap();
        std::fs::write(
            root.path().join(".env.example"),
            "USER_SETTING=new-default\n",
        )
        .unwrap();

        assert_eq!(
            seed_env_if_missing(root.path()).unwrap(),
            EnvSeed::Preserved
        );
        assert_eq!(
            std::fs::read_to_string(root.path().join(".env")).unwrap(),
            "USER_SETTING=keep-me\n"
        );
    }

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

    #[test]
    fn rejects_hash_prefixed_channel_name() {
        let err = validate_slack_channel("#testing").unwrap_err().to_string();
        assert!(err.contains("channel ID"), "{err}");
    }

    #[test]
    fn accepts_channel_id() {
        assert!(validate_slack_channel("C0BF2CL1U2F").is_ok());
    }

    #[test]
    fn rejects_leading_whitespace_hash() {
        assert!(validate_slack_channel("  #testing").is_err());
    }

    #[test]
    fn suppress_credential_forwards_nothing_even_with_byo() {
        // A fake OR local model run needs no credential: forward none, even when
        // an explicit BYO reference is present, so a real token never leaks into
        // the untrusted runner.
        assert_eq!(
            select_passthrough_env(true, Some("sk-or-x")),
            Vec::<String>::new()
        );
    }

    #[test]
    fn explicit_byo_credential_forwarded_alone() {
        // A non-empty BYO credential is forwarded alone -- the ambient SDK vars
        // must not shadow the operator's chosen credential.
        assert_eq!(
            select_passthrough_env(false, Some("sk-or-x")),
            vec!["AGENTOS_CREDENTIALS".to_string()]
        );
    }

    #[test]
    fn empty_byo_credential_falls_back_to_sdk_vars() {
        // An empty AGENTOS_CREDENTIALS (a blank line in .env) is treated as unset,
        // so the ambient SDK vars carry the legacy real-Anthropic credential.
        assert_eq!(
            select_passthrough_env(false, Some("")),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string()
            ]
        );
    }

    #[test]
    fn no_byo_credential_falls_back_to_sdk_vars() {
        assert_eq!(
            select_passthrough_env(false, None),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string()
            ]
        );
    }

    #[test]
    fn secret_env_appends_after_the_model_credential() {
        // --secret names ride alongside the model credential, in order, so an
        // authed MCP server gets its token next to the model token.
        assert_eq!(
            merge_secret_env(
                select_passthrough_env(false, None),
                &["GITHUB_PERSONAL_ACCESS_TOKEN".to_string()]
            ),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string(),
                "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
            ]
        );
    }

    #[test]
    fn secret_env_forwarded_even_when_model_credential_suppressed() {
        // A fake/local model suppresses the model credential but a bundle's MCP
        // secret must still reach the sandbox.
        assert_eq!(
            merge_secret_env(
                select_passthrough_env(true, None),
                &["GITHUB_PERSONAL_ACCESS_TOKEN".to_string()]
            ),
            vec!["GITHUB_PERSONAL_ACCESS_TOKEN".to_string()]
        );
    }

    #[test]
    fn secret_env_deduplicates_against_the_credential_vars() {
        // Passing a model-credential var as --secret must not duplicate it.
        assert_eq!(
            merge_secret_env(
                select_passthrough_env(false, None),
                &["ANTHROPIC_API_KEY".to_string()]
            ),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn deploy_names_the_remediation_when_api_is_unreachable() {
        let dir = tempfile::tempdir().unwrap();
        crate::scaffold::scaffold(dir.path(), "test-agent").unwrap();
        let hint = "kubectl -n agentos port-forward svc/agentos-api 8000:8000";
        let opts = super::DeployOpts {
            plugin_dir: dir.path().to_path_buf(),
            // port 1 is reserved/closed -> deterministic connection refused
            api_url: "http://127.0.0.1:1".to_string(),
            api_key: "k".to_string(),
            slack_channel: None,
            env: super::DeployEnv::Dev,
            label: Some("v0".to_string()),
            secret: vec![],
            secret_binding_supported: true,
            connect_hint: hint.to_string(),
        };
        let err = super::deploy(opts).await.unwrap_err();
        let rendered = format!("{err:#}");
        assert!(
            rendered.contains(hint),
            "hint missing from error: {rendered}"
        );
    }

    #[test]
    fn unbound_declared_secrets_diffs_declared_against_bound() {
        // All declared names bound -> nothing unbound.
        assert!(super::unbound_declared_secrets(
            &["GH_TOKEN".to_string()],
            &["GH_TOKEN".to_string()]
        )
        .is_empty());
        // A declared name not in the bound set is returned.
        assert_eq!(
            super::unbound_declared_secrets(
                &["GH_TOKEN".to_string(), "SLACK".to_string()],
                &["GH_TOKEN".to_string()]
            ),
            vec!["SLACK".to_string()]
        );
        // Nothing declared -> nothing unbound (even with bound extras).
        assert!(super::unbound_declared_secrets(&[], &["GH_TOKEN".to_string()]).is_empty());
        // The #464 mismatch: declared the connector name, bound a different one.
        assert_eq!(
            super::unbound_declared_secrets(
                &["GITHUB_PERSONAL_ACCESS_TOKEN".to_string()],
                &["GH_TOKEN".to_string()]
            ),
            vec!["GITHUB_PERSONAL_ACCESS_TOKEN".to_string()]
        );
        // A MALFORMED declared name (not env-var syntax) is excluded from the
        // gap: it is the plugin-format validator's job to reject it server-side,
        // so the gate must not preempt that with a misleading `--secret` message.
        assert!(super::unbound_declared_secrets(&["github-token".to_string()], &[]).is_empty());
        // A well-formed unbound name alongside a malformed one: only the
        // well-formed one is a gap.
        assert_eq!(
            super::unbound_declared_secrets(
                &["github-token".to_string(), "GITHUB_TOKEN".to_string()],
                &[]
            ),
            vec!["GITHUB_TOKEN".to_string()]
        );
    }

    #[tokio::test]
    async fn deploy_fails_when_declared_secret_is_not_bound() {
        // AC3: a declared secret NAME with no matching --secret binding fails the
        // deploy BEFORE any network attempt -- a true deploy-time error, not a
        // runtime/connection failure.
        let dir = tempfile::tempdir().unwrap();
        // Declares a NAME we will bind under the wrong key.
        scaffold_with_secrets(dir.path(), "test-agent", &["GITHUB_PERSONAL_ACCESS_TOKEN"]);

        let opts = super::DeployOpts {
            plugin_dir: dir.path().to_path_buf(),
            api_url: "http://127.0.0.1:1".to_string(),
            api_key: "k".to_string(),
            slack_channel: None,
            env: super::DeployEnv::Dev,
            label: Some("v0".to_string()),
            secret: vec!["GH_TOKEN".to_string()],
            secret_binding_supported: true,
            connect_hint: "UNREACHABLE-HINT-SENTINEL".to_string(),
        };
        let err = super::deploy(opts).await.unwrap_err();
        let rendered = format!("{err:#}");
        assert!(
            rendered.contains("GITHUB_PERSONAL_ACCESS_TOKEN"),
            "error must name the missing secret: {rendered}"
        );
        assert!(
            !rendered.contains("UNREACHABLE-HINT-SENTINEL"),
            "gate must fire before any network attempt (no connect hint): {rendered}"
        );
    }

    #[tokio::test]
    async fn deploy_skips_secrets_gate_when_binding_unsupported() {
        // AC2: the cluster tier cannot bind a `--secret` until #440, so the
        // declared-secrets gate is SKIPPED there. A secrets-declaring bundle must
        // NOT be preempted by the gate; deploy proceeds to the network and fails
        // on the connect path instead (naming the connect hint, never the secret).
        let dir = tempfile::tempdir().unwrap();
        scaffold_with_secrets(dir.path(), "test-agent", &["GITHUB_PERSONAL_ACCESS_TOKEN"]);

        let opts = super::DeployOpts {
            plugin_dir: dir.path().to_path_buf(),
            // port 1 is reserved/closed -> deterministic connection refused
            api_url: "http://127.0.0.1:1".to_string(),
            api_key: "k".to_string(),
            slack_channel: None,
            env: super::DeployEnv::Dev,
            label: Some("v0".to_string()),
            secret: vec![],
            secret_binding_supported: false,
            connect_hint: "UNREACHABLE-HINT-SENTINEL".to_string(),
        };
        let err = super::deploy(opts).await.unwrap_err();
        let rendered = format!("{err:#}");
        // The error is the network/connect path, not the secrets gate.
        assert!(
            rendered.contains("UNREACHABLE-HINT-SENTINEL"),
            "gate should be skipped, so deploy reaches the network: {rendered}"
        );
        assert!(
            !rendered.contains("GITHUB_PERSONAL_ACCESS_TOKEN"),
            "the skipped gate must not name the declared secret: {rendered}"
        );
    }
}
