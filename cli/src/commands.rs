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
use crate::evals::{graded_answer, load_suite, turn_passes, EvalSuite};
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

    crate::ui::ui().emit(&CheckOutput { report: &report });
    check_outcome(&report).map_err(anyhow::Error::from)
}

/// Output of `skill check` (#474): the MCP-load report, structured under `--json`
/// and rendered line-by-line otherwise, routed through the one `Ui::emit` point.
/// Borrows the report so the caller can still pass it to `check_outcome`.
struct CheckOutput<'a> {
    report: &'a CheckReport,
}

impl crate::ui::CliOutput for CheckOutput<'_> {
    fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self.report).unwrap_or_else(|_| serde_json::json!({}))
    }

    fn render(&self, ui: &crate::ui::Ui) {
        let report = self.report;
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
            spec.name.clone(),
            Some(spec_path.clone()),
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
        name.clone(),
        None,
        format!("initialized plugin bundle '{name}' in {}", dir.display()),
        created,
        &dir,
    );
    Ok(())
}

/// Report a freshly scaffolded bundle through the one success-path decision point
/// (`Ui::emit`, issue #485): under `--json` emit one structured `InitOutput`
/// object to stdout; otherwise render the success line, a `created` note per
/// written path, and the `Next:` hint on stderr (byte-identical to before).
/// Shared by both `init` branches so the only per-branch difference is the
/// success message text and whether a spec sourced the bundle.
fn report_scaffold(
    ui: &crate::ui::Ui,
    name: String,
    from_spec: Option<PathBuf>,
    success_msg: String,
    created: Vec<PathBuf>,
    dir: &Path,
) {
    ui.emit(&InitOutput {
        name,
        dir: dir.to_path_buf(),
        from_spec,
        created,
        success_msg,
    });
}

/// The result of `agentos init` (both the plain-name and `--from-spec` branches),
/// carried through `Ui::emit`. Under `--json` an agent gets the bundle name, the
/// directory, the spec source (null for the plain-name path), the list of created
/// paths, and the next-step command -- never empty stdout (issue #485). Owns its
/// data so `to_json`/`render` outlive the scaffold call.
pub struct InitOutput {
    pub name: String,
    pub dir: PathBuf,
    pub from_spec: Option<PathBuf>,
    pub created: Vec<PathBuf>,
    pub success_msg: String,
}

impl InitOutput {
    /// The copy-pasteable next-step command. The dir is shell-quoted (only when
    /// it carries a special char -- a kebab bundle name stays bare) so a path
    /// with a space yields a valid `cd`, not a broken two-token one. Shared by
    /// `to_json` and `render` so the machine and human forms never drift.
    fn next_command(&self) -> String {
        format!(
            "cd {} && agentos skill up",
            crate::ops::shell_quote(&self.dir.display().to_string())
        )
    }
}

impl crate::ui::CliOutput for InitOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "initialized": true,
            "name": self.name,
            "dir": self.dir.display().to_string(),
            "from_spec": self.from_spec.as_ref().map(|p| p.display().to_string()),
            "created": self
                .created
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>(),
            "next": self.next_command(),
        })
    }

    fn render(&self, ui: &crate::ui::Ui) {
        ui.success(&self.success_msg);
        for path in &self.created {
            ui.note(&format!("created {}", path.display()));
        }
        ui.note(&format!("Next: {}", self.next_command()));
    }
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
    let runner_image = docker::RUNNER_IMAGE;
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
        build(docker::RUNNER_IMAGE).await?;
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

/// `agentos dev bump-version <X.Y.Z>`: set the release-coupled version across
/// cli/Cargo.toml + Chart.yaml version/appVersion in one shot, so a release cut
/// cannot leave the three out of sync (the drift the #489 consistency gate
/// catches). It rewrites ONLY the line-anchored release fields (never a
/// dependency `version = ` line), refreshes the CLI lockfile, and prints the
/// commit + tag follow-up -- it does not commit, tag, or push. `--dry-run` prints
/// the planned edits and writes nothing.
pub async fn bump_version(version: &str, dry_run: bool) -> Result<()> {
    let ui = crate::ui::ui();
    // semver X.Y.Z with an optional -rc.N (the only pre-release shape we cut).
    let semver = regex::Regex::new(r"^\d+\.\d+\.\d+(-rc\.\d+)?$").expect("static regex");
    if !semver.is_match(version) {
        return Err(crate::exit::usage(format!(
            "version {version:?} must be semver X.Y.Z or X.Y.Z-rc.N"
        )));
    }
    let root = find_repo_root().context(
        "runner/Dockerfile not found here or in any parent directory. Run `agentos dev \
         bump-version` from an agentos source checkout.",
    )?;

    let cargo_path = root.join("cli/Cargo.toml");
    let chart_path = root.join("charts/agentos/Chart.yaml");
    let cargo = std::fs::read_to_string(&cargo_path)
        .with_context(|| format!("reading {}", cargo_path.display()))?;
    let chart = std::fs::read_to_string(&chart_path)
        .with_context(|| format!("reading {}", chart_path.display()))?;

    // Line-anchored so a dependency `version = "x"` line is never touched: only
    // the first `version = ` at column 0 (the [package] version) is rewritten.
    let cargo_new = replace_first_line(&cargo, "version = ", &format!("version = \"{version}\""))
        .context("cli/Cargo.toml has no top-level `version = ` line")?;
    let chart_new_v = replace_first_line(&chart, "version:", &format!("version: {version}"))
        .context("Chart.yaml has no `version:` line")?;
    let chart_new = replace_first_line(
        &chart_new_v,
        "appVersion:",
        &format!("appVersion: \"{version}\""),
    )
    .context("Chart.yaml has no `appVersion:` line")?;

    if dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: vec![
                format!("cli/Cargo.toml: version = \"{version}\""),
                format!("charts/agentos/Chart.yaml: version: {version}"),
                format!("charts/agentos/Chart.yaml: appVersion: \"{version}\""),
                "cargo update -p agentos (refresh Cargo.lock)".to_string(),
            ],
        });
        return Ok(());
    }

    std::fs::write(&cargo_path, cargo_new)
        .with_context(|| format!("writing {}", cargo_path.display()))?;
    std::fs::write(&chart_path, chart_new)
        .with_context(|| format!("writing {}", chart_path.display()))?;
    ui.note(&format!(
        "set version {version} in cli/Cargo.toml and charts/agentos/Chart.yaml"
    ));

    // Refresh the CLI lockfile so the committed Cargo.lock matches the new crate
    // version. Best-effort: a missing cargo or offline registry must not fail the
    // bump (the fields are already written); warn and let the operator run it.
    let lock_ok = tokio::process::Command::new("cargo")
        .args(["update", "-p", "agentos", "--precise", version])
        .current_dir(root.join("cli"))
        .status()
        .await
        .map(|s| s.success())
        .unwrap_or(false);
    if !lock_ok {
        ui.warn(
            "could not refresh cli/Cargo.lock automatically; run `cargo update -p agentos` in cli/",
        );
    }

    ui.emit(&BumpVersionOutput {
        version: version.to_string(),
    });
    Ok(())
}

/// Replace the first line beginning with `prefix` (after optional leading
/// whitespace) with `replacement`, preserving the line's indentation. Returns
/// None when no such line exists.
fn replace_first_line(content: &str, prefix: &str, replacement: &str) -> Option<String> {
    let mut out = Vec::new();
    let mut replaced = false;
    for line in content.lines() {
        if !replaced && line.trim_start().starts_with(prefix) {
            let indent = &line[..line.len() - line.trim_start().len()];
            out.push(format!("{indent}{replacement}"));
            replaced = true;
        } else {
            out.push(line.to_string());
        }
    }
    if !replaced {
        return None;
    }
    let mut joined = out.join("\n");
    if content.ends_with('\n') {
        joined.push('\n');
    }
    Some(joined)
}

/// Output of `dev bump-version`: the version now set across the release fields.
#[derive(Debug)]
pub struct BumpVersionOutput {
    pub version: String,
}

impl crate::ui::CliOutput for BumpVersionOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({"version": self.version})
    }

    fn render(&self, ui: &crate::ui::Ui) {
        ui.payload(&format!("bumped release version to {}", self.version));
        ui.note(&format!(
            "commit the change, then tag it: git commit -am \"release {v}\" && git tag v{v}",
            v = self.version
        ));
    }
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
/// selection (apps/worker/src/agentos_worker/sandbox/docker.py:199-207), which is
/// the authority this function mirrors. Three states:
/// - `fake_model`: forward NONE, and fake dominates every other state -- a fake
///   runner resolves no Anthropic credential, and a real token must not sit in an
///   untrusted, egress-rail-less container readable via /proc/1/environ.
/// - an explicit non-empty AGENTOS_CREDENTIALS (`byo_credential`): the operator's
///   chosen BYO credential, forwarded ALONE so an ambient SDK token can neither
///   shadow it nor ride into the sandbox. Kept even under a `base_url_override`:
///   the runner routes an sk-or- OpenRouter key into ANTHROPIC_API_KEY with a
///   preset base URL, so dropping it would break BYO OpenRouter.
/// - otherwise: the ambient SDK creds for the legacy real-Anthropic path, each
///   only when `ambient_present` reports it, and only when there is no
///   `base_url_override` -- a local endpoint needs no real Anthropic token.
///
/// The rule is frozen as data in tests/vectors/model-credential-forwarding.json,
/// which both this lane and the worker lane assert against: changing the rule
/// here without changing the worker (or the vectors) fails that gate (issue #495).
fn select_passthrough_env(
    fake_model: bool,
    base_url_override: bool,
    byo_credential: Option<&str>,
    ambient_present: &dyn Fn(&str) -> bool,
) -> Vec<String> {
    if fake_model {
        return Vec::new();
    }
    if byo_credential.is_some_and(|c| !c.is_empty()) {
        return vec!["AGENTOS_CREDENTIALS".into()];
    }
    if base_url_override {
        return Vec::new();
    }
    ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
        .into_iter()
        .filter(|name| ambient_present(name))
        .map(String::from)
        .collect()
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

/// The ambient-presence rule `select_passthrough_env` selects on.
///
/// Presence must match what `StartSpec::run_args` later filters the NAMES on
/// (docker.rs:117), or selection and emission disagree.
fn ambient_present_for(docker_env: &[(String, String)]) -> impl Fn(&str) -> bool + '_ {
    move |name| std::env::var_os(name).is_some() || stored_env_contains(docker_env, name)
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
    // `--local-model` is a base-URL override, not a fake-model run: it keeps an
    // explicit BYO credential (the runner routes it at the local endpoint) and
    // drops only the ambient SDK fallback. Derive both states the way the
    // container actually gets them, so the seam cannot drift from the argv.
    let fake_model = opts.local_model.is_none() && opts.fake_model;
    let base_url_override = model_base_url.is_some();
    let mut docker_env = Vec::new();
    if !fake_model {
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
    // Scoped so the borrow of `docker_env` ends before it is moved into the spec.
    let passthrough_env = {
        let ambient_present = ambient_present_for(&docker_env);
        merge_secret_env(
            select_passthrough_env(
                fake_model,
                base_url_override,
                byo_credential.as_deref(),
                &ambient_present,
            ),
            &opts.secret,
        )
    };

    let spec = StartSpec {
        image: opts.image.clone(),
        container_name: opts.name.clone(),
        host_port: opts.port,
        plugin_dir: plugin_dir.clone(),
        session_id: session_id.clone(),
        sandbox_id: "local".into(),
        budget_json: opts.budget,
        fake_model,
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

/// One graded eval case: `(id, passed, seconds, output)`. `output` is the graded
/// answer text (the reply `turn_passes`/`reply_passes` judged), carried so a red
/// case is diagnosable from `--json` without a manual re-run (#548). Shared by the
/// skill runner path and the local/cluster message path so both report the same
/// shape through `report_eval`/`eval_json`.
pub type EvalRow = (String, bool, f64, String);

/// The `agentos skill eval --json` payload: the pass/fail roll-up plus one row
/// per case. Pure so it stays unit/contract-testable against
/// `cli/schema/eval.schema.json`.
pub fn eval_json(results: &[EvalRow], passed: usize, total: usize) -> serde_json::Value {
    let cases: Vec<serde_json::Value> = results
        .iter()
        .map(|(id, ok, seconds, output)| {
            serde_json::json!({ "id": id, "passed": ok, "seconds": seconds, "output": output })
        })
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
    crate::ui::ui().emit(&StatusOutput {
        url,
        status: serde_json::to_value(&status)?,
    });
    Ok(())
}

/// Output of `skill status` (#474). `to_json` delegates to the schema-gated
/// `status_json` builder (byte-identical, so `cli/schema/status.schema.json` and
/// `json_contract.rs` stay green); `render` reproduces the note + pretty payload.
struct StatusOutput {
    url: String,
    status: serde_json::Value,
}

impl crate::ui::CliOutput for StatusOutput {
    fn to_json(&self) -> serde_json::Value {
        status_json(&self.url, &self.status)
    }

    fn render(&self, ui: &crate::ui::Ui) {
        ui.note(&format!("runner {}", self.url));
        ui.payload_plain(
            &serde_json::to_string_pretty(&self.status).unwrap_or_else(|_| self.status.to_string()),
        );
    }
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

    // Under `--json`, answer tokens are suppressed on stdout (they route through
    // `ui.answer`), so a streamed turn would exit 0 with empty stdout (#485).
    // Accumulate the full reply and emit one JSON object at the end instead. The
    // human path is unchanged: it streams live and this buffer is never emitted.
    let json = ui.json();
    let mut reply = String::new();

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
                    if json {
                        reply.push_str(&token);
                    } else {
                        ui.answer(&token);
                    }
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
        // Under `--json`, emit the one buffered turn object (reply + final
        // status) rather than the streamed/human trailer (#485). Emit BEFORE any
        // exit so a classified failure still carries its data to the consumer.
        if json {
            ui.emit(&SkillMessageOutput {
                reply: std::mem::take(&mut reply),
                status: status_str(status).to_string(),
                finalized: true,
            });
            if *status == SessionStatus::ClassifiedFailure {
                std::process::exit(1);
            }
            return Ok(());
        }
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

/// Output of `skill message` under `--json`: the full buffered reply plus the
/// final session status. The human path streams tokens live and never builds
/// this; it exists so `--json` emits one object instead of empty stdout (#485).
#[derive(Debug)]
pub struct SkillMessageOutput {
    pub reply: String,
    pub status: String,
    pub finalized: bool,
}

impl crate::ui::CliOutput for SkillMessageOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "reply": self.reply,
            "status": self.status,
            "finalized": self.finalized,
        })
    }

    fn render(&self, ui: &crate::ui::Ui) {
        // Only reached if a caller routes this through the human path; mirror the
        // streamed output as a single block for completeness.
        ui.answer(&self.reply);
        ui.note(&format!("-- final ({})", self.status));
    }
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
) -> Result<Vec<EvalRow>> {
    let mut results = Vec::with_capacity(suite.cases.len());
    for (i, case) in suite.cases.iter().enumerate() {
        // Fresh conversation by default (#550): reset the runner before a case so
        // it cannot answer from an earlier case's history instead of actually
        // invoking its tools. A shared_history case skips the reset and inherits
        // the prior case's conversation on purpose (a multi-turn scenario).
        if !case.shared_history {
            client.reset().await.with_context(|| {
                format!(
                    "resetting the runner conversation before case {:?}",
                    case.id
                )
            })?;
        }
        let started = Instant::now();
        let events = client
            .send_event(EventType::EvalCase, &case.input, "U-eval", |_| {})
            .await?;
        let elapsed = started.elapsed().as_secs_f64();
        // Capture the graded answer -- the exact text `turn_passes` judged -- so a
        // red case can be diagnosed from `--json` without a manual re-run (#548).
        results.push((
            case.id.clone(),
            turn_passes(case, &events),
            elapsed,
            graded_answer(&events),
        ));
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
    // Scoped so the borrow of `docker_env` ends before it is moved into the spec.
    let passthrough_env = {
        let ambient_present = ambient_present_for(&docker_env);
        merge_secret_env(
            select_passthrough_env(false, false, byo_credential.as_deref(), &ambient_present),
            secrets,
        )
    };
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
        let passed = results.iter().filter(|(_, ok, _, _)| *ok).count();
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
pub fn report_eval(results: &[EvalRow]) -> Result<()> {
    let total = results.len();
    let passed = results.iter().filter(|(_, ok, _, _)| *ok).count();
    // Emit through the one success point (#474), then apply the exit-code side
    // effect for BOTH paths -- the json path had it inline, the human path after.
    crate::ui::ui().emit(&EvalOutput {
        results,
        passed,
        total,
    });
    if passed < total {
        std::process::exit(crate::exit::ExitClass::Failure.code());
    }
    Ok(())
}

/// Output of `<tier> eval` (#474). `to_json` delegates to the schema-gated
/// `eval_json` builder (byte-identical, so `cli/schema/eval.schema.json` and
/// `json_contract.rs` stay green); `render` reproduces the per-case table and the
/// roll-up verdict + per-red-case reply notes.
struct EvalOutput<'a> {
    results: &'a [EvalRow],
    passed: usize,
    total: usize,
}

impl crate::ui::CliOutput for EvalOutput<'_> {
    fn to_json(&self) -> serde_json::Value {
        eval_json(self.results, self.passed, self.total)
    }

    fn render(&self, ui: &crate::ui::Ui) {
        let (results, passed, total) = (self.results, self.passed, self.total);
        let rows: Vec<Vec<String>> = results
            .iter()
            .map(|(name, ok, seconds, _)| {
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
            // Surface WHAT each red case actually replied, so a human need not
            // re-run by hand to see why it failed (#548). Empty means the turn
            // never produced gradeable text (no `done`/reply) -- the diagnosis.
            for (name, _, _, output) in results.iter().filter(|(_, ok, _, _)| !*ok) {
                let shown = if output.is_empty() {
                    "<no reply text>".to_string()
                } else {
                    output.clone()
                };
                ui.note(&format!("{name} replied: {shown}"));
            }
            ui.warn(&format!(
                "{passed}/{total} passed; {} failed",
                total - passed
            ));
        }
    }
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

pub async fn deploy(opts: DeployOpts) -> Result<DeployOutput> {
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
    Ok(DeployOutput {
        plugin_name,
        label,
        env: env.to_string(),
        agent_name: outcome.agent.name,
        agent_id: outcome.agent.id,
        version_label: outcome.version.version_label,
        version_id: outcome.version.id,
        channel,
        bundle_ref: outcome.bundle.bundle_ref,
        bundle_sha256: outcome.bundle.bundle_sha256,
        bundle_size_bytes: outcome.bundle.size_bytes,
        deployment_id: outcome.deployment.id,
        deployment_environment: outcome.deployment.environment,
        deployment_status: outcome.deployment.status,
    })
}

/// Output of `<tier> deploy`: the deployed agent/version/channel/bundle/deployment
/// summary. Owns its data so `to_json`/`render` outlive the `ApiClient`; the
/// json-vs-human choice is made once in `Ui::emit` (#456, #485). Without this the
/// real-path success emitted only `payload`/`kv`, which suppress under `--json`,
/// so `deploy --json` exited 0 with empty stdout.
#[derive(Debug)]
pub struct DeployOutput {
    pub plugin_name: String,
    pub label: String,
    pub env: String,
    pub agent_name: String,
    pub agent_id: String,
    pub version_label: String,
    pub version_id: String,
    pub channel: String,
    pub bundle_ref: String,
    pub bundle_sha256: String,
    pub bundle_size_bytes: u64,
    pub deployment_id: String,
    pub deployment_environment: String,
    pub deployment_status: String,
}

impl crate::ui::CliOutput for DeployOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "plugin": self.plugin_name,
            "label": self.label,
            "environment": self.env,
            "agent": {"name": self.agent_name, "id": self.agent_id},
            "version": {"label": self.version_label, "id": self.version_id},
            "channel": self.channel,
            "bundle": {
                "ref": self.bundle_ref,
                "sha256": self.bundle_sha256,
                "size_bytes": self.bundle_size_bytes,
            },
            "deployment": {
                "id": self.deployment_id,
                "environment": self.deployment_environment,
                "status": self.deployment_status,
            },
        })
    }

    fn render(&self, ui: &crate::ui::Ui) {
        ui.payload(&format!(
            "deployed {} {} -> {}",
            self.plugin_name, self.label, self.env
        ));
        ui.kv(
            "agent",
            &format!("{} ({})", self.agent_name, ui.url(&self.agent_id)),
        );
        ui.kv(
            "version",
            &format!("{} ({})", self.version_label, ui.url(&self.version_id)),
        );
        ui.kv("channel", &self.channel);
        ui.kv(
            "bundle",
            &format!(
                "{} sha256:{} {} bytes",
                self.bundle_ref, self.bundle_sha256, self.bundle_size_bytes
            ),
        );
        ui.kv(
            "deployment",
            &format!(
                "{} [{}] {}",
                self.deployment_id, self.deployment_environment, self.deployment_status
            ),
        );
    }
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
                            "bundle_sha256": v.bundle_sha256,
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
                    // Show the bundle hash consistently across tiers (#548): it is
                    // the parity evidence, so a human-readable listing must carry it
                    // too, not just `cluster deploy`'s printout.
                    let sha = v.bundle_sha256.as_deref().unwrap_or("-");
                    ui.kv(
                        &v.version_label,
                        &format!("sha256 {sha}  commit {commit}  by {by}  at {at}"),
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

/// The pending-list / resolve flags for `local approvals` (#506). Defaulted so
/// the skill/cluster tiers, which keep only the gate view/set surface, pass an
/// empty value.
#[derive(Default)]
pub struct ApprovalCmd {
    pub list: bool,
    pub resolve: Option<String>,
    pub as_actor: Option<String>,
    pub reject: bool,
    pub note: Option<String>,
}

/// Output of `<tier> approvals <agent>`: the dry-run plan, the gate list (empty
/// vec == "no tools gated"), the pending records, or a resolved record (#506).
/// Owns its data so it outlives the `ApiClient`.
///
/// `manifest_unreadable` carries the third gate-list state (#607): `gated_tools`
/// alone cannot distinguish "the deployed bundle manifest declares no gates" from
/// "the manifest could not be read at all" -- both used to arrive as an empty vec
/// and render as the affirmative "calls run without approval". `Some(reason)`
/// means the manifest lookup failed, so the list is what we could see rather than
/// what is armed. Always `None` on the set path (`--gate`/`--clear`).
pub enum ApprovalsOutput {
    DryRun(crate::ui::DryRunPlan),
    Gates {
        agent: String,
        gated_tools: Vec<String>,
        manifest_unreadable: Option<String>,
    },
    Pending {
        agent: String,
        records: Vec<crate::api::ApprovalRecord>,
    },
    Resolved {
        record: crate::api::ApprovalRecord,
    },
}

fn approval_record_json(r: &crate::api::ApprovalRecord) -> serde_json::Value {
    serde_json::json!({
        "id": r.id,
        "author": r.author,
        "route": r.route,
        "gate_kind": r.gate_kind,
        "granted_tool": r.granted_tool,
        "status": r.status,
        "conversation_id": r.conversation_id,
        "summary": r.summary,
        "expires_at": r.expires_at,
        "resolved_by": r.resolved_by,
    })
}

impl crate::ui::CliOutput for ApprovalsOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ApprovalsOutput::DryRun(plan) => plan.to_json(),
            ApprovalsOutput::Gates {
                agent,
                gated_tools,
                manifest_unreadable,
            } => serde_json::json!({
                "agent": agent,
                "gated_tools": gated_tools,
                "manifest_unreadable": manifest_unreadable,
            }),
            ApprovalsOutput::Pending { agent, records } => serde_json::json!({
                "agent": agent,
                "pending": records.iter().map(approval_record_json).collect::<Vec<_>>(),
            }),
            ApprovalsOutput::Resolved { record } => serde_json::json!({
                "resolved": approval_record_json(record),
            }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ApprovalsOutput::DryRun(plan) => plan.render(ui),
            ApprovalsOutput::Gates {
                agent,
                gated_tools,
                manifest_unreadable,
            } => {
                ui.payload(&approvals_summary_line(
                    agent,
                    gated_tools,
                    manifest_unreadable.as_deref(),
                ));
                for tool in gated_tools {
                    ui.kv("gated", tool);
                }
            }
            ApprovalsOutput::Pending { agent, records } => {
                if records.is_empty() {
                    ui.payload(&format!("{agent}: no pending approvals"));
                } else {
                    ui.payload(&format!("{agent} — {} pending approval(s):", records.len()));
                    for r in records {
                        let tool = r.granted_tool.as_deref().unwrap_or("-");
                        let route = r.route.as_deref().unwrap_or("(requesting channel)");
                        ui.kv(
                            &r.id,
                            &format!(
                                "{} — {} [tool: {tool}, route: {route}, by: {}]",
                                r.summary, r.conversation_id, r.author
                            ),
                        );
                    }
                }
            }
            ApprovalsOutput::Resolved { record } => {
                ui.payload(&format!(
                    "approval {} -> {} (by {})",
                    record.id,
                    record.status,
                    record.resolved_by.as_deref().unwrap_or("?")
                ));
            }
        }
    }
}

/// The human summary line for `<tier> approvals`' gate view.
///
/// Four lines for three states, because whether gates were found is orthogonal to
/// whether we could read the manifest that declares them. The `unreadable` arm is
/// the one that matters: an unanswered lookup must not borrow the vocabulary of an
/// answered one. Reporting "no tools are gated (calls run without approval)"
/// because the deployment list request errored tells the reader the runner will
/// not pause, which is a claim this command never checked -- and the reader acts on
/// it. Same reasoning as the skill tier's `gates_summary_line`, where the unseen
/// source is the boot-time env override rather than the deployed manifest.
///
/// The unreadable-with-gates arm is not redundant: gates from the platform's
/// `approval_required_tools` field are real, but presenting them without the
/// caveat implies the list is the whole set.
fn approvals_summary_line(agent: &str, gated_tools: &[String], unreadable: Option<&str>) -> String {
    match (gated_tools.is_empty(), unreadable) {
        (true, None) => format!("{agent}: no tools are gated (calls run without approval)"),
        (false, None) => format!("{agent} — {} gated tool(s):", gated_tools.len()),
        (true, Some(reason)) => format!(
            "{agent}: the deployed bundle manifest could not be read ({reason}), so whether it \
             gates any tool is unknown. The platform's approval_required_tools field lists none, \
             which is not the same as nothing being gated"
        ),
        (false, Some(reason)) => format!(
            "{agent} — {} gated tool(s), and this list may be incomplete: the deployed bundle \
             manifest could not be read ({reason}), so any gate it declares is not shown:",
            gated_tools.len()
        ),
    }
}

/// `<tier> approvals <agent> [--gate TOOL]... [--clear]`: view or set the tool
/// names whose calls pause for human approval. No flags => show current gates.
pub async fn approvals(
    opts: AgentActionOpts,
    gate: Vec<String>,
    clear: bool,
    cmd: ApprovalCmd,
) -> Result<ApprovalsOutput> {
    let gate_mode = clear || !gate.is_empty();

    // --resolve <id> --as <user>: resolve one live approval record (#506). It is
    // id-scoped, not gate config, so it is mutually exclusive with --gate/--clear/
    // --list. Approve by default; --reject rejects.
    if let Some(approval_id) = cmd.resolve {
        if gate_mode || cmd.list {
            return Err(crate::exit::usage(
                "--resolve cannot be combined with --gate/--clear/--list",
            ));
        }
        let actor = cmd.as_actor.ok_or_else(|| {
            crate::exit::usage("--resolve requires --as <user> (the actor resolving it)")
        })?;
        let decision = if cmd.reject { "rejected" } else { "approved" };
        if opts.dry_run {
            return Ok(ApprovalsOutput::DryRun(crate::ui::DryRunPlan {
                lines: vec![format!(
                    "POST {}/approvals/{approval_id}/resolve decision={decision} resolved_by={actor:?}",
                    opts.api_url
                )],
            }));
        }
        let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
        let record = client
            .resolve_approval(&approval_id, decision, &actor, cmd.note.as_deref())
            .await?;
        return Ok(ApprovalsOutput::Resolved { record });
    }

    // --list: the agent's pending approval records (#506).
    if cmd.list {
        if gate_mode {
            return Err(crate::exit::usage(
                "--list cannot be combined with --gate/--clear",
            ));
        }
        if opts.dry_run {
            return Ok(ApprovalsOutput::DryRun(crate::ui::DryRunPlan {
                lines: vec![format!(
                    "GET {}/approvals?status_filter=pending&agent_id=<id>  (would resolve agent {:?} first)",
                    opts.api_url, opts.agent
                )],
            }));
        }
        let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
        let agent = client.find_agent(&opts.agent).await?;
        let records = client.list_pending_approvals(&agent.id).await?;
        return Ok(ApprovalsOutput::Pending {
            agent: agent.name,
            records,
        });
    }

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
    let (gates, unreadable) = if setting {
        let cl = ui.checklist();
        let step = cl.step(&format!("updating approval gates for {}", agent.name));
        match client.set_approval_tools(&agent.id, &gate).await {
            Ok(updated) => {
                step.done("updated");
                (updated.approval_required_tools.unwrap_or_default(), None)
            }
            Err(err) => {
                step.fail("failed");
                return Err(err);
            }
        }
    } else {
        // Report what the runner actually arms (#546): the UNION of the platform's
        // mutable `approval_required_tools` field (delivered as
        // AGENTOS_APPROVAL_REQUIRED_TOOLS) AND the in-force deployed bundle
        // manifest's `approvalPolicy` gates. Reading only the API field reported an
        // empty set while a manifest gate was armed and blocking.
        //
        // When that manifest half cannot be read, the union is only the field half
        // and the report says so (#607) rather than passing a partial answer off as
        // the effective set.
        let mut gated = agent.approval_required_tools.clone().unwrap_or_default();
        match deployed_manifest_gate_names(&client, &agent.id).await? {
            ManifestGates::Readable(names) => {
                for name in names {
                    if !gated.contains(&name) {
                        gated.push(name);
                    }
                }
                (gated, None)
            }
            ManifestGates::Unreadable(reason) => (gated, Some(reason)),
        }
    };
    Ok(ApprovalsOutput::Gates {
        agent: agent.name,
        gated_tools: gates,
        manifest_unreadable: unreadable,
    })
}

/// Shared flags for the console verbs (`<tier> console login|revoke`, #630).
///
/// Deliberately not `AgentActionOpts`: a console session belongs to the INSTALL,
/// not to an agent, so there is no agent to resolve and no `agent` field to
/// carry. Both verbs authenticate with the platform key, which is the whole
/// point of ADR-0049 -- session management is reachable only from where the key
/// already lives, so a session can never mint its own successor.
pub struct ConsoleOpts {
    pub api_url: String,
    pub api_key: String,
    pub dry_run: bool,
}

/// Output of `<tier> console login`: the dry-run plan, or the minted code.
///
/// Owns its data so `to_json`/`render` outlive the `ApiClient`. What is printed
/// here is the single-use CODE, never the platform key: the code is a
/// short-lived credential for exactly one session, and printing it is the
/// mechanism by which the operator never has to handle the key.
#[derive(Debug)]
pub enum ConsoleLoginOutput {
    DryRun(crate::ui::DryRunPlan),
    Minted {
        code: String,
        expires_at: String,
        session_id: String,
        /// The console to paste the code into, when it could be resolved. An
        /// `Option` rather than a hard failure: a code we minted is valid even
        /// if we cannot name the URL, and failing the mint over a cosmetic
        /// lookup would throw away a live credential.
        console_url: Option<String>,
        /// Set when `console_url`'s origin is one the login exchange refuses (a
        /// plaintext NodePort console): the loopback path that can accept this
        /// code. `None` when the console URL is already loginable.
        login: Option<crate::ops::ConsoleLoginPath>,
    },
}

impl crate::ui::CliOutput for ConsoleLoginOutput {
    /// `{"code","expires_at","session_id","console_url":<string|null>,
    /// "login":{"url","port_forward"}|null}`.
    ///
    /// `console_url` and `login` are emitted with an explicit null rather than
    /// omitted when unresolved / not needed, per the repo convention pinned by
    /// `kill_output_json_shape_is_pinned`. `login` is structured data, not a
    /// hint buried in prose: an agent under `--json` reads `login.url` as the
    /// URL this code can be spent at (#630, ADR-0049).
    fn to_json(&self) -> serde_json::Value {
        match self {
            ConsoleLoginOutput::DryRun(plan) => plan.to_json(),
            ConsoleLoginOutput::Minted {
                code,
                expires_at,
                session_id,
                console_url,
                login,
            } => serde_json::json!({
                "code": code,
                "expires_at": expires_at,
                "session_id": session_id,
                "console_url": console_url,
                "login": login.as_ref().map(|l| serde_json::json!({
                    "url": l.url,
                    "port_forward": l.port_forward,
                })),
            }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ConsoleLoginOutput::DryRun(plan) => plan.render(ui),
            ConsoleLoginOutput::Minted {
                code,
                expires_at,
                console_url,
                login,
                ..
            } => {
                ui.kv("login code", code);
                ui.kv("expires at", expires_at);
                match console_url {
                    Some(url) => ui.kv("console", &ui.url(url)),
                    None => ui.note(
                        "could not resolve the console URL; find it with `agentos cluster status`",
                    ),
                }
                // The console URL above is a plaintext origin, so the exchange
                // would refuse this code there. Name the URL it CAN be spent at,
                // at the exact moment the operator is about to spend it.
                if let Some(login) = login {
                    ui.kv(
                        "log in at",
                        &format!("{}  then {}", login.port_forward, ui.url(&login.url)),
                    );
                }
                ui.note("single use: open the console, paste the code, and it is spent");
            }
        }
    }
}

/// `<tier> console login`: mint a single-use login code for the console
/// (`POST /console/login-codes`, #630/ADR-0049).
///
/// `access` is resolved by the caller because the tiers resolve it differently
/// (a fixed loopback localhost URL, already a secure context, vs the release's
/// `ui` service, which may be a plaintext NodePort the exchange refuses), which
/// is the same split `observability` already draws between the two tiers.
pub async fn console_login(
    opts: ConsoleOpts,
    access: crate::ops::ConsoleAccess,
) -> Result<ConsoleLoginOutput> {
    if opts.dry_run {
        return Ok(ConsoleLoginOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!("POST {}/console/login-codes", opts.api_url)],
        }));
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let minted = client.mint_console_login_code().await?;
    Ok(ConsoleLoginOutput::Minted {
        code: minted.code,
        expires_at: minted.expires_at,
        session_id: minted.session_id,
        console_url: access.url,
        login: access.login,
    })
}

/// Output of `<tier> console revoke`: the dry-run plan, or the revoked count.
#[derive(Debug)]
pub enum ConsoleRevokeOutput {
    DryRun(crate::ui::DryRunPlan),
    Done { revoked: u64 },
}

impl crate::ui::CliOutput for ConsoleRevokeOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ConsoleRevokeOutput::DryRun(plan) => plan.to_json(),
            ConsoleRevokeOutput::Done { revoked } => serde_json::json!({ "revoked": revoked }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ConsoleRevokeOutput::DryRun(plan) => plan.render(ui),
            ConsoleRevokeOutput::Done { revoked } => {
                ui.payload(&format!("revoked {revoked} console session(s)"));
                ui.note("mint a new way in with `console login`");
            }
        }
    }
}

/// `<tier> console revoke`: revoke every live console session
/// (`DELETE /console/sessions`, #630/ADR-0049).
///
/// No `--yes` confirmation gate, deliberately diverging from `kill`: this is the
/// operator's kill switch for a credential they believe is compromised, and a
/// prompt between them and revocation is the wrong thing to put there. It is
/// also cheap to undo -- `console login` mints a new way in -- which is what
/// makes the asymmetry with `kill` (which stops an agent's runs) correct.
pub async fn console_revoke(opts: ConsoleOpts) -> Result<ConsoleRevokeOutput> {
    if opts.dry_run {
        return Ok(ConsoleRevokeOutput::DryRun(crate::ui::DryRunPlan {
            lines: vec![format!("DELETE {}/console/sessions", opts.api_url)],
        }));
    }
    let client = ApiClient::new(&opts.api_url, &opts.api_key)?;
    let revoked = client.revoke_console_sessions().await?;
    Ok(ConsoleRevokeOutput::Done { revoked })
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

// --- skill tier parity (issue #459) -----------------------------------------

/// The env var the runner reads for the operator's approval-gate override.
const APPROVAL_TOOLS_ENV: &str = "AGENTOS_APPROVAL_REQUIRED_TOOLS";

/// The manifest locations the runner's `load_approval_policy` probes, in order.
const MANIFEST_LOCATIONS: [&str; 2] = [".claude-plugin/plugin.json", "plugin.json"];

/// One `approvalPolicy.gates[]` entry: the tool the runner intercepts plus the
/// approval route the platform binds per deployment. A local mirror of
/// `plugin_format.models.ApprovalGate`, kept private here so `scaffold`'s
/// `read_manifest` (used by `skill up`/`skill check`) keeps its narrow shape.
///
/// Both fields are `Option` so a MISSING key stays distinguishable from one
/// present but empty. Since #520 both refuse the manifest (the runner arms a
/// declared policy exactly or not at all), so the distinction no longer changes
/// the verdict -- but it still names the actual defect in the error, which is
/// the difference between a fixable message and a puzzle. `#[serde(default)]`
/// on a `String` would collapse them and report "empty" for a key that is
/// simply absent.
#[derive(Deserialize)]
struct ApprovalGateDecl {
    gate: Option<String>,
    route: Option<String>,
}

/// The manifest `approvalPolicy` object; mirrors `plugin_format.models.ApprovalPolicy`.
#[derive(Deserialize, Default)]
struct ApprovalPolicyDecl {
    #[serde(default)]
    gates: Vec<ApprovalGateDecl>,
}

/// Just the slice of the plugin manifest this verb reads.
///
/// `name` is carried only to mirror `plugin_format.models.PluginManifest`, whose
/// sole required field it is: without it `model_validate` raises and the runner
/// arms zero gates, so a narrower struct that parsed happily would report gates
/// the runner never arms.
///
/// KNOWN LIMITATION (ADR-0041): this validates only the approval-relevant subset
/// of the manifest. The runner parses the WHOLE `PluginManifest`, so a manifest
/// that is valid here but invalid in an unrelated modeled field (say `commands:
/// 123`) makes `load_approval_policy` return `{}` -- the runner arms ZERO gates
/// while this view still lists them. Closing that properly needs a shared or
/// drift-gated manifest parser; hand-mirroring every `PluginManifest` field here
/// would just add a second ungated mirror to drift.
#[derive(Deserialize)]
struct ManifestApprovals {
    name: Option<String>,
    #[serde(rename = "approvalPolicy")]
    approval_policy: Option<ApprovalPolicyDecl>,
}

/// Output of `skill approvals`: the bundle's declared gates, or the env
/// assignment a set/clear WOULD need (this tier boots the runner from env, so
/// there is nothing to mutate; see ADR-0041).
#[derive(Debug)]
pub enum SkillApprovalsOutput {
    Gates {
        gates: Vec<(String, String)>,
    },
    Env {
        env: String,
        restart: String,
        bundle_note: String,
    },
}

impl crate::ui::CliOutput for SkillApprovalsOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            SkillApprovalsOutput::Gates { gates } => {
                let gates: Vec<serde_json::Value> = gates
                    .iter()
                    .map(|(gate, route)| serde_json::json!({"gate": gate, "route": route}))
                    .collect();
                serde_json::json!({ "gates": gates })
            }
            SkillApprovalsOutput::Env {
                env,
                restart,
                bundle_note,
            } => serde_json::json!({
                "env": env,
                "restart": restart,
                "bundle_note": bundle_note,
            }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            SkillApprovalsOutput::Gates { gates } => {
                ui.payload(&gates_summary_line(gates));
                for (gate, route) in gates {
                    ui.kv(gate, route);
                }
            }
            SkillApprovalsOutput::Env {
                env,
                restart,
                bundle_note,
            } => {
                ui.payload(&human_env_line(env));
                ui.kv("restart", restart);
                ui.kv("bundle", bundle_note);
            }
        }
    }
}

/// The human-rendered form of the `NAME=value` assignment `skill approvals`
/// hands back.
///
/// The guidance tells the caller to export this, so the human line is read as
/// shell text and the value must survive being pasted into one. A gate name is
/// only rejected here for a comma or for being whitespace-only, so `--gate 'Foo
/// Bar'` or `--gate '$(cmd)'` are accepted and would otherwise be word-split or
/// command-substituted by the shell -- the runner would receive a different gate
/// than the one printed, or the paste would execute shell text outright.
/// Quoting the right-hand side is what keeps the printed line and the applied
/// value the same string.
///
/// The `--json` `env` field deliberately does NOT get this treatment: a machine
/// consumer wants the raw assignment, not a shell literal it would have to
/// unquote.
fn human_env_line(env: &str) -> String {
    match env.split_once('=') {
        Some((name, value)) => format!("{name}={}", shell_quote(value)),
        // Not reachable from `skill_approvals` (it always formats `NAME=`), but
        // echoing the input beats inventing an assignment that was never made.
        None => env.to_string(),
    }
}

/// The human summary line for `skill approvals`' gate view.
///
/// Scoped to what this command actually knows: it reads the bundle on disk and
/// nothing else. The runner also unions in `AGENTOS_APPROVAL_REQUIRED_TOOLS`,
/// resolved once at container boot and invisible from here, so neither branch may
/// present the bundle's gates as the complete effective set. Saying "no gates
/// declared, so calls run without approval" would be flatly false against a runner
/// booted with that override set.
fn gates_summary_line(gates: &[(String, String)]) -> String {
    let unseen = "an AGENTOS_APPROVAL_REQUIRED_TOOLS override applied at container boot may gate more, and is not visible from the bundle";
    if gates.is_empty() {
        format!("the bundle declares no approval gates ({unseen})")
    } else {
        format!("{} bundle-declared gate(s) ({unseen}):", gates.len())
    }
}

/// Read the bundle's declared approval gates as `(gate, route)` pairs.
///
/// The manifest is probed at `.claude-plugin/plugin.json` then `plugin.json`,
/// mirroring the runner's `load_approval_policy`. Since #520 that function is
/// single-tier and fail-closed: ANY gate it cannot arm exactly as declared --
/// a required key missing (the manifest's `name`, or a gate's `gate`/`route`),
/// or a key present but empty/whitespace so it keys nothing -- raises rather
/// than degrading to "nothing gated". So both shapes are reported here as one
/// usage error naming the problem. The manifest is invalid input, deterministic
/// and fixable by hand; reporting an empty list instead would read as "no gates
/// configured", a different lie (#607).
///
/// A manifest with no `approvalPolicy` at all, or an explicitly empty `gates`
/// list, declares no gate: no gates and no error. A bundle with no manifest is a
/// usage error (the plugin dir is simply wrong).
fn read_bundle_gates(plugin_dir: &Path) -> Result<Vec<(String, String)>> {
    let manifest_path = MANIFEST_LOCATIONS
        .iter()
        .map(|loc| plugin_dir.join(loc))
        .find(|path| path.is_file())
        .ok_or_else(|| {
            crate::exit::usage(format!(
                "no plugin manifest under {}: expected .claude-plugin/plugin.json or plugin.json",
                plugin_dir.display()
            ))
        })?;
    let body = std::fs::read_to_string(&manifest_path)
        .with_context(|| format!("reading {}", manifest_path.display()))?;
    parse_manifest_gates(&body, &manifest_path.display().to_string())
}

/// Parse the `approvalPolicy` gates out of a plugin-manifest JSON body, mirroring
/// the runner's `load_approval_policy` fail-closed semantics (#520): any declared
/// gate the runner cannot arm exactly as declared -- a missing REQUIRED key, or a
/// key present but empty -- refuses the whole manifest rather than arming a
/// subset, so this reports a usage error for both. `source` labels the manifest in
/// errors. Shared by the skill tier (manifest on local disk) and the local/cluster
/// tiers (manifest pulled from the deployed bundle over the API, #546) so both
/// read gates identically.
fn parse_manifest_gates(body: &str, source: &str) -> Result<Vec<(String, String)>> {
    let invalid = |problem: &str| {
        crate::exit::usage(format!(
            "invalid plugin manifest {source}: {problem}. The runner rejects this manifest and arms ZERO approval gates, including any well-formed ones"
        ))
    };
    let manifest: ManifestApprovals =
        serde_json::from_str(body).map_err(|e| invalid(&format!("not valid JSON ({e})")))?;
    if manifest.name.is_none() {
        return Err(invalid("missing the required `name` field"));
    }
    let mut gates = Vec::new();
    for g in manifest.approval_policy.unwrap_or_default().gates {
        let (gate, route) = match (&g.gate, &g.route) {
            (None, _) => return Err(invalid("a gate in `approvalPolicy.gates` omits `gate`")),
            (_, None) => return Err(invalid("a gate in `approvalPolicy.gates` omits `route`")),
            (Some(gate), Some(route)) => (gate.trim(), route.trim()),
        };
        // Present but empty: parses, but keys nothing once trimmed, so the
        // runner refuses to boot rather than arm a partial policy (#520).
        // Reporting it as armed here would name a gate that stops the runner.
        if gate.is_empty() || route.is_empty() {
            return Err(invalid(
                "a gate in `approvalPolicy.gates` has an empty `gate` or `route`",
            ));
        }
        // The runner keys a dict by the trimmed gate name, so a repeated gate
        // collapses to one entry: the first declaration fixes the position, the
        // last one wins the route. Mirror both halves -- keeping the duplicate
        // would report a gate the runner never arms plus a stale route.
        match gates
            .iter_mut()
            .find(|(name, _): &&mut (String, String)| name == gate)
        {
            Some((_, existing_route)) => *existing_route = route.to_string(),
            None => gates.push((gate.to_string(), route.to_string())),
        }
    }
    Ok(gates)
}

/// The active deployment whose bundle is in force for an agent: prod outranks dev
/// (mirroring `binding.py`), then most recent. `list_deployments` returns rows
/// oldest-first, so "most recent" is the last match. `None` when the agent has no
/// active deployment (nothing is running its bundle yet).
fn select_in_force_deployment(
    deployments: &[crate::api::Deployment],
) -> Option<&crate::api::Deployment> {
    let active: Vec<&crate::api::Deployment> = deployments
        .iter()
        .filter(|d| d.status == "active")
        .collect();
    active
        .iter()
        .rev()
        .find(|d| d.environment == "prod")
        .or_else(|| active.iter().rev().find(|d| d.environment == "dev"))
        .or_else(|| active.last())
        .copied()
}

/// The approval-gate tool names armed by the agent's in-force DEPLOYED bundle
/// manifest (#546): resolve the active deployment → its version → the version's
/// stored manifest → `approvalPolicy.gates[].gate`. This is the source the runner
/// consults that the platform's mutable `approval_required_tools` field does NOT
/// carry, so `local`/`cluster approvals` must union it in or it reports an empty
/// gate set while the manifest gate is armed and blocking. Best-effort on the
/// fetch (no deployment / no bundle / API hiccup → no manifest gates), but a
/// deployed manifest that is actually invalid is surfaced (it disarms every gate).
///
/// The empty-vec outcomes are NOT interchangeable, which is why this returns
/// `ManifestGates` rather than a bare list (#607): "the manifest declares nothing"
/// is an answer, "the API call failed" is the absence of one, and the caller's
/// report reads very differently for each.
enum ManifestGates {
    /// The lookup completed. The vec is the manifest's armed gates, empty when
    /// there is no deployed bundle, no manifest in it, or no `approvalPolicy`.
    Readable(Vec<String>),
    /// The lookup did not complete, so the manifest's gates are unknown. Carries
    /// the reason, which is reported rather than swallowed.
    Unreadable(String),
}

async fn deployed_manifest_gate_names(client: &ApiClient, agent_id: &str) -> Result<ManifestGates> {
    let deployments = match client.list_deployments(agent_id).await {
        Ok(d) => d,
        Err(err) => {
            return Ok(ManifestGates::Unreadable(format!(
                "listing the agent's deployments failed: {err}"
            )))
        }
    };
    // No active deployment is a real answer: nothing is running this agent's
    // bundle, so no manifest gate can be armed from one.
    let Some(deployment) = select_in_force_deployment(&deployments) else {
        return Ok(ManifestGates::Readable(Vec::new()));
    };
    // A deployment IS in force but names no version. `version_id` is
    // `#[serde(default)]`, so this is response drift rather than a stated absence
    // -- the bundle exists and we simply cannot address it.
    let Some(version_id) = deployment.version_id.clone() else {
        return Ok(ManifestGates::Unreadable(format!(
            "the in-force deployment {} reports no version id",
            deployment.id
        )));
    };
    let files = match client.bundle_files(agent_id, &version_id).await {
        Ok(f) => f,
        Err(err) => {
            return Ok(ManifestGates::Unreadable(format!(
                "fetching the deployed bundle's files failed: {err}"
            )))
        }
    };
    let Some(manifest) = files
        .iter()
        .find(|f| MANIFEST_LOCATIONS.contains(&f.path.as_str()))
    else {
        return Ok(ManifestGates::Readable(Vec::new()));
    };
    let gates = parse_manifest_gates(
        &manifest.content,
        &format!("deployed bundle manifest ({})", manifest.path),
    )?;
    Ok(ManifestGates::Readable(
        gates.into_iter().map(|(gate, _route)| gate).collect(),
    ))
}

/// POSIX-shell-quote a value for safe interpolation into emitted shell text.
///
/// Two callers, both emitting text a caller reads as shell: the bundle path named
/// by the `restart` guidance, and the right-hand side of the human-rendered
/// `NAME=value` assignment the guidance says to export. A value holding
/// whitespace or shell metacharacters (`/tmp/my bundle`, `$(cmd)`) would
/// otherwise be word-split or substituted, so what the shell sees differs from
/// what we printed. Single-quoting is the one POSIX form that quotes every
/// character literally; the only byte it cannot contain is
/// `'` itself, which is escaped by closing the quote, emitting an escaped quote,
/// and reopening (`'\''`). Done by hand rather than by pulling in a crate: the
/// rule is four lines and a dependency here is not worth the supply-chain surface.
///
/// Not shared with `ops::shell_quote`, which is deliberately different: that one
/// leaves shell-safe tokens bare because it renders helm `--set` argv for humans
/// to read, where quoting every token is noise. This one always quotes, because
/// these values are copied into a shell and an unquoted one is a silent
/// mis-target rather than a visible mistake.
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', r"'\''"))
}

/// `skill approvals [--plugin-dir DIR] [--gate TOOL]... [--clear]`: view the
/// bundle's declared approval gates, or print the env assignment that sets or
/// clears the runner's override.
///
/// Unlike the `local`/`cluster` tiers there is no platform record to PATCH: this
/// tier's runner resolves `AGENTOS_APPROVAL_REQUIRED_TOOLS` once at container
/// boot. So set/clear mutate nothing and instead hand back the assignment plus
/// the two caveats that make it honest (issue #459).
pub async fn skill_approvals(
    plugin_dir: PathBuf,
    gate: Vec<String>,
    clear: bool,
) -> Result<SkillApprovalsOutput> {
    if clear && !gate.is_empty() {
        return Err(crate::exit::usage(
            "--clear cannot be combined with --gate (clear removes the env override)",
        ));
    }
    for g in &gate {
        if g.trim().is_empty() {
            return Err(crate::exit::usage("--gate cannot be empty"));
        }
        if g.contains(',') {
            return Err(crate::exit::usage(format!(
                "--gate {g:?} cannot contain a comma: {APPROVAL_TOOLS_ENV} is comma-separated"
            )));
        }
    }
    if !clear && gate.is_empty() {
        return Ok(SkillApprovalsOutput::Gates {
            gates: read_bundle_gates(&plugin_dir)?,
        });
    }
    // The set/clear path emits guidance that names this bundle and tells the
    // caller to re-boot a runner for it, so it must be at least as sure the
    // bundle exists as the view path is -- otherwise `--plugin-dir /does/not/exist`
    // exits 0 with instructions that fail at `skill up`, which is the tier-parity
    // lie this command exists to avoid (issue #459). Same resolution and same
    // validation as the view path, deliberately reusing the one function so the
    // two paths cannot diverge on what counts as a usable bundle. A manifest
    // present and valid but declaring no `approvalPolicy` yields an empty list
    // and no error: setting an override for a bundle that declares no gates is
    // exactly the legitimate case, so only a missing, unreadable, or invalid
    // manifest is rejected. The gates themselves are irrelevant here; the call is
    // for its validation.
    read_bundle_gates(&plugin_dir)?;
    let tools: Vec<&str> = gate.iter().map(|g| g.trim()).collect();
    Ok(SkillApprovalsOutput::Env {
        env: format!("{APPROVAL_TOOLS_ENV}={}", tools.join(",")),
        // This states the MECHANISM and the DELTA; it deliberately does not
        // synthesize a command line to paste. `skill up` carries the runner's
        // whole configuration in its flags (`StartOpts`: image, port, name,
        // network, otel_endpoint, budget, model, local_model, fake_model, and
        // repeatable secret), and `skill approvals` reads only the bundle on
        // disk -- it has no idea which of those the caller passed. A synthesized
        // `skill up --secret ...` would therefore re-boot the runner on DEFAULTS
        // plus the approval var: a different model provider, a different image
        // and port, and every other `--secret` connector credential silently
        // dropped. Naming the caller's own invocation as the thing to re-run is
        // the only form that stays true without knowing it.
        //
        // The clauses that remain are each verifiable:
        // 1. `skill up` forwards an env var into the runner only when its NAME is
        //    on the passthrough list, and the model-credential names are all that
        //    list holds by default (`select_passthrough_env`). `--secret NAME`
        //    appends to it (`merge_secret_env`), so a re-run without it arms
        //    nothing.
        // 2. `start` hard-errors when a runner is already recorded for the dir, so
        //    an existing runner must be stopped first.
        // 3. `stop` takes no args and hardcodes `Path::new(".")`, so `skill down`
        //    can only act on the bundle in the CWD -- there is no `--plugin-dir`
        //    for it. Naming the bundle dir (shell-quoted, since it is read as a
        //    path in shell text) tells a caller working elsewhere which bundle
        //    this output is about.
        restart: format!(
            "env resolves once at container boot, so nothing changes until the runner re-boots. This output is about the bundle at {}. To apply it: export the assignment above, then re-run your own original `agentos skill up` invocation for that bundle with `--secret {APPROVAL_TOOLS_ENV}` added -- a plain `agentos skill up` does not forward it. This command cannot see how that runner was started, so re-run your invocation rather than a fresh one, which would boot on defaults and drop your other flags. Stop an already-recorded runner first with `agentos skill down`, run from that bundle directory (it takes no --plugin-dir and acts on the bundle in the current directory).",
            shell_quote(&plugin_dir.display().to_string())
        ),
        // The runner UNIONS the bundle's declared gates with this env override,
        // so saying only "set/cleared" would lie by omission about what is armed.
        bundle_note: if clear {
            "clears only the env override; gates declared in the bundle manifest stay armed"
                .to_string()
        } else {
            "adds to the gates declared in the bundle manifest; it cannot remove one".to_string()
        },
    })
}

// The reason/alternative for each tier-unavailable skill verb has TWO consumers:
// the runtime `{error, fix}` payload built by `exit::unsupported` below, and the
// clap `about` text in `main.rs` that flows into the committed
// `command-manifest.json` (the discovery surface the UI parity mirror reads).
// Nothing gates prose against prose, so they are single-sourced here: a stale
// help string is the same class of lie as a stale runtime answer, just on the
// discovery surface (issue #459, ADR-0041).

/// Why `skill versions` cannot be answered at this tier.
pub const VERSIONS_REASON: &str =
    "`skill up` runs the bundle bytes on disk, so no deployed version is assigned";
/// Where to run `versions` instead.
pub const VERSIONS_ALT: &str =
    "use `agentos local versions <agent>` or `agentos cluster versions <agent>` for a deployed agent";
/// Why `skill memory` cannot be answered at this tier.
pub const MEMORY_REASON: &str =
    "this tier configures no memory namespace: `skill up` never sets a memory ref, and there is no platform here to own or address one";
/// Where to run `memory` instead.
pub const MEMORY_ALT: &str =
    "use `agentos local memory <agent>` or `agentos cluster memory <agent>` for a deployed agent";

/// `skill versions`: answered, but unavailable at this tier by construction.
///
/// A version exists only because the platform assigns a `bundle_sha256` and a
/// `version_label` at deploy; `skill up` runs whatever bytes are on disk, so
/// there is no release to inspect here (issue #459, ADR-0041).
pub fn skill_versions_unavailable() -> anyhow::Error {
    crate::exit::unsupported("versions", VERSIONS_REASON, VERSIONS_ALT)
}

/// `skill memory`: answered, but not a capability of this tier.
///
/// Memory is a namespace some *platform* provisions, addresses, and owns; the
/// `local`/`cluster` tiers have one, and this tier has none. `skill up` never
/// sets `AGENTOS_MEMORY_REF`, so the runner it boots resolves a
/// `NullMemoryStore` and nothing is persisted.
///
/// Deliberately NOT phrased as "cannot exist by construction". `--secret` has no
/// reserved-name fence (`merge_secret_env`), so an operator CAN hand-forward
/// `--secret AGENTOS_MEMORY_REF --secret AGENTOS_MEMORY_TOKEN` and the runner's
/// `resolve_memory` will dereference an `http(s)://` ref into a real
/// `StateApiMemoryStore`. That escape hatch is an operator wiring a foreign
/// tier's namespace through this one by hand -- not this tier growing the
/// capability -- and this command could not report on it regardless: it has no
/// way to read a running container's env. So the verb stays unavailable (exit 4)
/// and the reason claims only what is true: the tier configures no namespace
/// (issue #459, ADR-0041).
pub fn skill_memory_unavailable() -> anyhow::Error {
    crate::exit::unsupported("memory", MEMORY_REASON, MEMORY_ALT)
}

#[cfg(test)]
mod tests {
    use super::{
        merge_secret_env, parse_manifest_gates, replace_first_line, resolve_cases_path,
        seed_env_if_missing, select_in_force_deployment, select_passthrough_env,
        validate_slack_channel, EnvSeed,
    };
    use serde::Deserialize;
    use std::path::{Path, PathBuf};

    #[test]
    fn replace_first_line_rewrites_only_the_first_anchored_line() {
        // The [package] version, not a dependency `version = ` line below it.
        let cargo = "[package]\nname = \"agentos\"\nversion = \"0.4.0\"\n\n[dependencies]\nserde = { version = \"1\" }\n";
        let out = replace_first_line(cargo, "version = ", "version = \"0.5.0\"").unwrap();
        assert!(out.contains("version = \"0.5.0\""));
        // The dependency's inline version is untouched.
        assert!(out.contains("serde = { version = \"1\" }"));
        assert_eq!(out.matches("0.5.0").count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn replace_first_line_preserves_indentation_and_reports_absence() {
        let chart = "apiVersion: v2\nname: agentos\nappVersion: \"0.4.0\"\n";
        let out = replace_first_line(chart, "appVersion:", "appVersion: \"0.5.0\"").unwrap();
        assert!(out.contains("appVersion: \"0.5.0\""));
        assert!(replace_first_line(chart, "nonexistent:", "x").is_none());
    }

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

    /// A fully-credentialed host, for the cases below that are not about which
    /// ambient names happen to be exported.
    fn all_ambient_present(_name: &str) -> bool {
        true
    }

    #[test]
    fn fake_model_forwards_nothing_even_with_byo() {
        // A fake model run needs no credential: forward none, even when an
        // explicit BYO reference is present, so a real token never leaks into
        // the untrusted runner.
        assert_eq!(
            select_passthrough_env(true, false, Some("sk-or-x"), &all_ambient_present),
            Vec::<String>::new()
        );
    }

    #[test]
    fn explicit_byo_credential_forwarded_alone() {
        // A non-empty BYO credential is forwarded alone -- the ambient SDK vars
        // must not shadow the operator's chosen credential.
        assert_eq!(
            select_passthrough_env(false, false, Some("sk-or-x"), &all_ambient_present),
            vec!["AGENTOS_CREDENTIALS".to_string()]
        );
    }

    #[test]
    fn empty_byo_credential_falls_back_to_sdk_vars() {
        // An empty AGENTOS_CREDENTIALS (a blank line in .env) is treated as unset,
        // so the ambient SDK vars carry the legacy real-Anthropic credential.
        assert_eq!(
            select_passthrough_env(false, false, Some(""), &all_ambient_present),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string()
            ]
        );
    }

    #[test]
    fn no_byo_credential_falls_back_to_sdk_vars() {
        assert_eq!(
            select_passthrough_env(false, false, None, &all_ambient_present),
            vec![
                "CLAUDE_CODE_OAUTH_TOKEN".to_string(),
                "ANTHROPIC_API_KEY".to_string()
            ]
        );
    }

    /// One row of the committed cross-language forwarding matrix. The five
    /// inputs are booleans: the rule keys on presence, never on a credential's
    /// content.
    ///
    /// `deny_unknown_fields` makes an unrecognized key a hard parse failure
    /// rather than a silently ignored input: a row that grows a sixth input
    /// this lane cannot see would otherwise pass vacuously, which is the exact
    /// drift the gate exists to catch. A new input must be taught to this
    /// struct, to the Python lane's expected key set
    /// (apps/worker/tests/sandbox/test_vector_credential_forwarding.py), and to
    /// the vector file itself.
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ForwardingVector {
        name: String,
        /// Documentation carried by the vector file; parsed so the row's own
        /// rationale is not an unknown field, and read back into the assertion
        /// message so a failing vector explains itself.
        why: String,
        fake_model: bool,
        base_url_override: bool,
        byo_credential: bool,
        ambient_oauth: bool,
        ambient_api_key: bool,
        expected: Vec<String>,
    }

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ForwardingVectors {
        /// The file-level rationale; parsed so it is not an unknown field.
        /// Underscore-prefixed so rustc's dead_code lint skips it; the serde
        /// rename keeps the JSON key it matches on as `comment`.
        #[serde(rename = "comment")]
        _comment: String,
        vectors: Vec<ForwardingVector>,
    }

    #[test]
    fn cli_matches_every_forwarding_vector() {
        // The Rust half of the cross-language gate (#495). The Python worker lane
        // (apps/worker/tests/sandbox/test_vector_credential_forwarding.py) reads
        // this same file, so a rule changed in one language without the other
        // fails that language's test. The rule is not restated here.
        let raw = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../tests/vectors/model-credential-forwarding.json"
        ))
        .expect("read tests/vectors/model-credential-forwarding.json");
        let parsed: ForwardingVectors = serde_json::from_str(&raw).unwrap_or_else(|err| {
            panic!(
                "parse tests/vectors/model-credential-forwarding.json: {err}\n\
                 An unknown field is rejected on purpose: a new input this lane cannot see \
                 would pass vacuously. Teach the new key to ForwardingVector here, to \
                 _EXPECTED_VECTOR_KEYS in \
                 apps/worker/tests/sandbox/test_vector_credential_forwarding.py, and to both \
                 implementations of the rule."
            )
        });
        // Guards against a rename or a truncated file making this loop vacuously pass.
        assert!(!parsed.vectors.is_empty(), "no vectors parsed");

        for vector in &parsed.vectors {
            let ambient_present = |name: &str| match name {
                "CLAUDE_CODE_OAUTH_TOKEN" => vector.ambient_oauth,
                "ANTHROPIC_API_KEY" => vector.ambient_api_key,
                _ => false,
            };
            let byo = vector.byo_credential.then_some("sk-or-PLACEHOLDER-byo");
            assert_eq!(
                select_passthrough_env(
                    vector.fake_model,
                    vector.base_url_override,
                    byo,
                    &ambient_present
                ),
                vector.expected,
                "{}: {}",
                vector.name,
                vector.why
            );
        }
    }

    #[test]
    fn secret_env_appends_after_the_model_credential() {
        // --secret names ride alongside the model credential, in order, so an
        // authed MCP server gets its token next to the model token.
        assert_eq!(
            merge_secret_env(
                select_passthrough_env(false, false, None, &all_ambient_present),
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
                select_passthrough_env(true, false, None, &all_ambient_present),
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
                select_passthrough_env(false, false, None, &all_ambient_present),
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

    // --- skill approvals (tier parity, issue #459) --------------------------

    /// Write a plugin manifest at `rel` under `dir`, creating parent dirs.
    fn write_manifest(dir: &std::path::Path, rel: &str, body: &str) {
        let path = dir.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(path, body).unwrap();
    }

    /// A minimal valid manifest, declaring no `approvalPolicy`.
    ///
    /// The set/clear path validates the bundle the same way the view path does,
    /// so every env-path test needs a real bundle on disk. Declaring no gates is
    /// the legitimate no-policy case, which that path must still accept.
    const MINIMAL_MANIFEST: &str = r#"{"name":"x","version":"1"}"#;

    /// Give `dir` the minimal valid bundle manifest the env path requires.
    fn write_minimal_manifest(dir: &std::path::Path) {
        write_manifest(dir, ".claude-plugin/plugin.json", MINIMAL_MANIFEST);
    }

    /// The gate names listed by a `skill approvals` view output's JSON.
    fn gate_names(json: &serde_json::Value) -> Vec<String> {
        json["gates"]
            .as_array()
            .expect("view JSON exposes a `gates` array")
            .iter()
            .map(|g| {
                g["gate"]
                    .as_str()
                    .expect("gate name is a string")
                    .to_string()
            })
            .collect()
    }

    fn usage_class(err: &anyhow::Error) -> crate::exit::ExitClass {
        crate::exit::classify(err).0
    }

    fn deployment(env: &str, status: &str, version: &str, ts: &str) -> crate::api::Deployment {
        crate::api::Deployment {
            id: format!("dep-{version}"),
            environment: env.into(),
            status: status.into(),
            version_id: Some(version.into()),
            deployed_at: Some(ts.into()),
        }
    }

    #[test]
    fn select_in_force_deployment_prefers_prod_then_most_recent() {
        // Oldest-first, mixed envs/statuses. prod outranks dev; among a rank the
        // most recent (last) active row wins; inactive rows are ignored (#546).
        let deps = vec![
            deployment("dev", "active", "v1", "2026-07-01"),
            deployment("prod", "superseded", "v2", "2026-07-02"),
            deployment("prod", "active", "v3", "2026-07-03"),
            deployment("dev", "active", "v4", "2026-07-04"),
        ];
        assert_eq!(
            select_in_force_deployment(&deps).and_then(|d| d.version_id.clone()),
            Some("v3".to_string()),
            "active prod wins over a newer active dev"
        );
        // No prod: newest active dev.
        let dev_only = vec![
            deployment("dev", "active", "a", "2026-07-01"),
            deployment("dev", "active", "b", "2026-07-05"),
        ];
        assert_eq!(
            select_in_force_deployment(&dev_only).and_then(|d| d.version_id.clone()),
            Some("b".to_string())
        );
        // No active deployment at all -> nothing in force.
        let none = vec![deployment("dev", "superseded", "x", "2026-07-01")];
        assert!(select_in_force_deployment(&none).is_none());
        assert!(select_in_force_deployment(&[]).is_none());
    }

    #[test]
    fn approvals_summary_line_never_claims_ungated_when_the_manifest_is_unreadable() {
        // The whole point of the three-state split (#607): "no gates found" and
        // "could not look" are different answers, and only the first one licenses
        // the affirmative claim. A failed manifest fetch used to collapse into the
        // second branch here and report the agent as running without approval.
        let ungated = super::approvals_summary_line("weather", &[], None);
        assert!(
            ungated.contains("no tools are gated (calls run without approval)"),
            "a genuinely readable, gate-free agent still gets the affirmative claim: {ungated}"
        );

        let blind = super::approvals_summary_line("weather", &[], Some("the deploy list failed"));
        assert!(
            !blind.contains("no tools are gated"),
            "an unreadable manifest must not render as an affirmative un-gated claim: {blind}"
        );
        assert!(
            blind.contains("could not be read") && blind.contains("the deploy list failed"),
            "the reason we could not look is disclosed: {blind}"
        );

        // Gates found from the platform field while the manifest was unreadable:
        // the list is real but partial, and silence about that implies complete.
        let partial =
            super::approvals_summary_line("weather", &["Bash".into()], Some("the fetch failed"));
        assert!(
            partial.contains("incomplete") && partial.contains("could not be read"),
            "a partial list discloses that more gates may be armed: {partial}"
        );

        let complete = super::approvals_summary_line("weather", &["Bash".into()], None);
        assert!(
            !complete.contains("incomplete") && complete.contains("1 gated tool(s)"),
            "a fully-read gate list makes no incompleteness caveat: {complete}"
        );
    }

    #[test]
    fn parse_manifest_gates_extracts_gate_route_pairs() {
        // The shared parser (#546) recovers approvalPolicy gates from raw manifest
        // text, the same shape `local`/`cluster approvals` union into the report.
        let gates = parse_manifest_gates(
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"mcp__plugin_gh_github__create_issue","route":"eng"}]}}"#,
            "test manifest",
        )
        .expect("valid manifest parses");
        assert_eq!(
            gates,
            vec![(
                "mcp__plugin_gh_github__create_issue".to_string(),
                "eng".to_string()
            )]
        );
        // A manifest missing the required `name` disarms every gate -> surfaced.
        assert!(parse_manifest_gates(
            r#"{"version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"}]}}"#,
            "bad manifest",
        )
        .is_err());
    }

    #[test]
    fn parse_manifest_gates_refuses_a_gate_the_runner_cannot_arm() {
        // NEGATIVE CONTROL for the #520 CLI mirror. The runner refuses to boot
        // on a declared gate it cannot arm, so reporting `Bash` as armed here
        // would name a gate that in fact stops the runner. Restoring the old
        // `continue` (drop the empty gate, keep its siblings) makes this fail.
        for body in [
            // Present but empty/whitespace: parses, keys nothing once trimmed.
            r#"{"name":"x","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"   ","route":"eng"}]}}"#,
            r#"{"name":"x","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"Write","route":""}]}}"#,
            // Required key missing entirely.
            r#"{"name":"x","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"Write"}]}}"#,
        ] {
            assert!(
                parse_manifest_gates(body, "partial manifest").is_err(),
                "reported gates for a manifest the runner refuses: {body}"
            );
        }
        // An explicitly empty gates list declares nothing: no gates, no error.
        assert_eq!(
            parse_manifest_gates(r#"{"name":"x","approvalPolicy":{"gates":[]}}"#, "empty")
                .expect("an empty gates list is a valid declaration of no gates"),
            Vec::new()
        );
    }

    #[tokio::test]
    async fn skill_approvals_view_lists_bundle_gates() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"mcp__x__y","route":"eng"}]}}"#,
        );
        let out = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap();
        let names = gate_names(&out.to_json());
        assert!(names.contains(&"Bash".to_string()), "{names:?}");
        assert!(names.contains(&"mcp__x__y".to_string()), "{names:?}");
    }

    #[tokio::test]
    async fn skill_approvals_view_reads_fallback_plugin_json() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            "plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"}]}}"#,
        );
        let out = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap();
        assert_eq!(gate_names(&out.to_json()), vec!["Bash".to_string()]);
    }

    #[tokio::test]
    async fn skill_approvals_view_empty_when_no_policy() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1"}"#,
        );
        let out = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap();
        assert!(gate_names(&out.to_json()).is_empty());
    }

    #[tokio::test]
    async fn skill_approvals_view_refuses_an_incomplete_gate() {
        let dir = tempfile::tempdir().unwrap();
        // The second gate has an empty route, so it keys nothing and the runner
        // refuses to boot on it (#520). Mirror that refusal: reporting `Bash` as
        // armed while the runner will not start is the drift this test pins.
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"NoRoute","route":""}]}}"#,
        );
        assert!(
            super::skill_approvals(dir.path().to_path_buf(), vec![], false)
                .await
                .is_err(),
            "reported gates for a manifest the runner refuses to boot on"
        );
    }

    #[tokio::test]
    async fn skill_approvals_view_duplicate_gate_collapses_to_last_route() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        // The runner keys a dict by trimmed gate name, so `Bash` declared twice
        // arms ONCE with the LAST route. Reporting both would name a gate the
        // runner never arms and a route it never fires.
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"stale"},{"gate":"Other","route":"ops"},{"gate":" Bash ","route":"eng"}]}}"#,
        );
        let out = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap();
        let json = out.to_json();
        let gates = json["gates"].as_array().unwrap();
        assert_eq!(
            gates.len(),
            2,
            "a gate declared twice must collapse to one entry, as the runner's dict does: {gates:?}"
        );
        let bash: Vec<&serde_json::Value> = gates.iter().filter(|g| g["gate"] == "Bash").collect();
        assert_eq!(bash.len(), 1, "exactly one Bash entry: {gates:?}");
        assert_eq!(
            bash[0]["route"], "eng",
            "the LAST declaration must win the route, mirroring the runner's dict comprehension: {gates:?}"
        );
        // First declaration fixes position, as Python dict insertion order does.
        assert_eq!(
            gates[0]["gate"], "Bash",
            "order must stay stable: {gates:?}"
        );
    }

    #[test]
    fn skill_approvals_render_never_claims_calls_are_ungated() {
        // The bundle is not the effective policy: AGENTOS_APPROVAL_REQUIRED_TOOLS
        // is resolved at container boot and cannot be seen from here, so neither
        // branch may imply the listed gates are the complete set.
        let empty = super::gates_summary_line(&[]);
        assert!(
            !empty.contains("without approval"),
            "an empty bundle policy must not claim calls run without approval -- an env override may gate them: {empty}"
        );
        assert!(
            empty.contains("AGENTOS_APPROVAL_REQUIRED_TOOLS"),
            "the empty render must name the override it cannot see: {empty}"
        );
        let listed = super::gates_summary_line(&[("Bash".into(), "eng".into())]);
        assert!(
            listed.contains("AGENTOS_APPROVAL_REQUIRED_TOOLS"),
            "the non-empty render must not imply the listed gates are the complete effective set: {listed}"
        );
    }

    // --- tier 1: a REQUIRED key missing disarms the WHOLE policy in the runner.
    // `plugin_format.models.ApprovalGate` declares `gate: str` / `route: str`
    // with no default, so `model_validate` raises and `load_approval_policy`
    // returns {} -- zero gates armed. Reporting the well-formed sibling as armed
    // would claim a safety control the runner never arms.

    #[tokio::test]
    async fn skill_approvals_view_gate_missing_route_key_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"gate":"NoRoute"}]}}"#,
        );
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
        // The sibling must not be reported as armed anywhere in the message.
        assert!(
            !format!("{err:#}").contains("Bash -> eng"),
            "a key-missing gate disarms every gate: {err:#}"
        );
    }

    #[tokio::test]
    async fn skill_approvals_view_gate_missing_gate_key_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x","version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"},{"route":"eng"}]}}"#,
        );
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_view_manifest_without_name_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        // `PluginManifest` requires `name`; without it the runner's parse raises
        // and it arms zero gates, so listing `Bash` here would be a false report.
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"version":"1","approvalPolicy":{"gates":[{"gate":"Bash","route":"eng"}]}}"#,
        );
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_view_malformed_json_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x",,}"#,
        );
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_view_without_manifest_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_set_emits_env_assignment() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_minimal_manifest(dir.path());
        let out = super::skill_approvals(
            dir.path().to_path_buf(),
            vec!["A".into(), "B".into()],
            false,
        )
        .await
        .unwrap();
        let json = out.to_json();
        assert_eq!(
            json["env"].as_str().unwrap(),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS=A,B"
        );
        let restart = json["restart"].as_str().unwrap();
        assert!(
            restart.contains("--secret AGENTOS_APPROVAL_REQUIRED_TOOLS"),
            "the restart caveat must name the --secret forwarding that actually applies the env, not a bare `skill up` (which forwards only model credentials): {restart}"
        );
        assert!(
            restart.contains("boot"),
            "the restart caveat must still say the env resolves once at container boot: {restart}"
        );
        assert!(
            restart.contains(&dir.path().display().to_string()),
            "the restart caveat must carry the caller's --plugin-dir so the re-boot targets the bundle whose approvals were read, not whatever bundle happens to be in the CWD: {restart}"
        );
        assert!(
            restart.contains("agentos skill down"),
            "the restart caveat must name the stop-first step: `start` hard-errors when a runner is already recorded for the dir: {restart}"
        );
        let bundle_note = json["bundle_note"].as_str().unwrap();
        assert!(
            bundle_note.contains("adds to") && bundle_note.contains("cannot remove"),
            "the set path's bundle note must state the add-only semantics (the runner unions the bundle gates with the override): {bundle_note}"
        );
    }

    #[tokio::test]
    async fn skill_approvals_clear_emits_empty_env_assignment() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_minimal_manifest(dir.path());
        let out = super::skill_approvals(dir.path().to_path_buf(), vec![], true)
            .await
            .unwrap();
        let json = out.to_json();
        assert_eq!(
            json["env"].as_str().unwrap(),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS="
        );
        let restart = json["restart"].as_str().unwrap();
        assert!(
            restart.contains("--secret AGENTOS_APPROVAL_REQUIRED_TOOLS"),
            "the clear path's restart caveat must name the --secret forwarding too: a bare `skill up` never forwards the cleared assignment either: {restart}"
        );
        assert!(
            restart.contains(&dir.path().display().to_string()),
            "the clear path's restart caveat must carry the caller's --plugin-dir too, or the re-boot clears the override on the wrong bundle: {restart}"
        );
        assert!(
            restart.contains("agentos skill down"),
            "the clear path's restart caveat must name the stop-first step too: {restart}"
        );
        let bundle_note = json["bundle_note"].as_str().unwrap();
        assert!(
            bundle_note.contains("only the env override") && bundle_note.contains("stay armed"),
            "the clear path's bundle note must state that it clears only the override and leaves the bundle-declared gates armed: {bundle_note}"
        );
    }

    /// `skill approvals` reads only the bundle on disk, so it cannot know which
    /// of `skill up`'s flags (image, port, name, network, otel-endpoint, budget,
    /// model, local-model, fake-model, repeatable --secret) the caller passed.
    /// A synthesized `skill up --secret ...` presented as the command to run is
    /// therefore actively destructive: following it re-boots the runner on
    /// defaults, switching model provider and dropping every other connector
    /// `--secret`. The guidance must point at the caller's OWN invocation.
    #[tokio::test]
    async fn skill_approvals_restart_points_at_the_callers_own_up_invocation() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_minimal_manifest(dir.path());
        for (gate, clear) in [(vec!["A".to_string()], false), (vec![], true)] {
            let out = super::skill_approvals(dir.path().to_path_buf(), gate, clear)
                .await
                .unwrap();
            let json = out.to_json();
            let restart = json["restart"].as_str().unwrap();
            assert!(
                !restart.contains("`agentos skill up --secret"),
                "the guidance must not synthesize a `skill up --secret ...` command line: this command cannot reconstruct the caller's original flags, so pasting it re-boots on defaults and drops their other --secret credentials (clear={clear}): {restart}"
            );
            assert!(
                restart.contains("your own original `agentos skill up` invocation"),
                "the guidance must direct the caller to re-run their own original invocation with the flag added (clear={clear}): {restart}"
            );
        }
    }

    #[tokio::test]
    async fn skill_approvals_restart_shell_quotes_a_bundle_path_with_a_space() {
        use crate::ui::CliOutput;
        // The guidance names the bundle dir inside shell-facing text, so a path
        // the shell would split must travel quoted or it names a different dir.
        let dir = tempfile::tempdir().unwrap();
        let spaced = dir.path().join("my bundle");
        std::fs::create_dir(&spaced).unwrap();
        write_minimal_manifest(&spaced);
        let out = super::skill_approvals(spaced.clone(), vec!["A".to_string()], false)
            .await
            .unwrap();
        let json = out.to_json();
        let restart = json["restart"].as_str().unwrap();
        assert!(
            restart.contains(&format!("'{}'", spaced.display())),
            "a bundle path containing a space must be emitted single-quoted: {restart}"
        );
    }

    /// The guidance says to export the assignment, so the human line is read as
    /// shell text. `--gate` rejects only commas and whitespace-only names, so a
    /// gate with a space reaches this line; unquoted, bash word-splits it and the
    /// runner is handed a different gate than the one printed.
    #[tokio::test]
    async fn skill_approvals_human_render_shell_quotes_an_assignment_with_a_space() {
        use crate::ui::CliOutput;
        let dir = tempfile::tempdir().unwrap();
        write_minimal_manifest(dir.path());
        let out = super::skill_approvals(dir.path().to_path_buf(), vec!["Foo Bar".into()], false)
            .await
            .unwrap();
        // The --json field stays the raw assignment: a machine consumer wants the
        // value, not a shell literal it would have to unquote.
        assert_eq!(
            out.to_json()["env"].as_str().unwrap(),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS=Foo Bar"
        );
        assert_eq!(
            super::human_env_line("AGENTOS_APPROVAL_REQUIRED_TOOLS=Foo Bar"),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS='Foo Bar'"
        );
        assert_eq!(
            super::human_env_line("AGENTOS_APPROVAL_REQUIRED_TOOLS=$(cmd)"),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS='$(cmd)'",
            "shell syntax in a gate name must be quoted, not left to be substituted on paste"
        );
        // The cleared assignment still renders as an assignment to an empty value.
        assert_eq!(
            super::human_env_line("AGENTOS_APPROVAL_REQUIRED_TOOLS="),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS=''"
        );
    }

    /// The console-login payload shape is a contract an agent parses (#630).
    /// `console_url` must be an explicit null when unresolved, never omitted --
    /// the repo convention pinned by `kill_output_json_shape_is_pinned`.
    #[test]
    fn console_login_json_shape_is_pinned() {
        use crate::ui::CliOutput;
        let out = super::ConsoleLoginOutput::Minted {
            code: "abc123".into(),
            expires_at: "2026-07-17T12:00:00Z".into(),
            session_id: "11111111-1111-1111-1111-111111111111".into(),
            console_url: None,
            login: None,
        };
        let json = out.to_json();
        assert!(
            json.get("console_url")
                .is_some_and(serde_json::Value::is_null),
            "an unresolved console_url must be an explicit null, not omitted: {json}"
        );
        assert!(
            json.get("login").is_some_and(serde_json::Value::is_null),
            "a console that needs no port-forward must say so with an explicit null: {json}"
        );
        assert_eq!(json["code"], "abc123");
        assert_eq!(json["expires_at"], "2026-07-17T12:00:00Z");
        assert_eq!(json["session_id"], "11111111-1111-1111-1111-111111111111");
    }

    #[tokio::test]
    async fn console_dry_run_plans_the_request_and_makes_none() {
        use crate::ui::CliOutput;
        // An unroutable URL: if a dry run ever sent a request, this would error
        // rather than return a plan, so the assertion proves no call was made.
        let opts = || super::ConsoleOpts {
            api_url: "http://127.0.0.1:1".into(),
            api_key: "K".into(),
            dry_run: true,
        };
        let login = super::console_login(opts(), Default::default())
            .await
            .unwrap();
        assert_eq!(login.to_json()["dry_run"], true);
        assert_eq!(
            login.to_json()["plan"][0],
            "POST http://127.0.0.1:1/console/login-codes"
        );

        let revoke = super::console_revoke(opts()).await.unwrap();
        assert_eq!(revoke.to_json()["dry_run"], true);
        assert_eq!(
            revoke.to_json()["plan"][0],
            "DELETE http://127.0.0.1:1/console/sessions"
        );
    }

    /// The minted code IS meant to be printed -- it is the credential that keeps
    /// the platform key out of the browser. The platform key never is.
    #[test]
    fn console_login_renders_the_code_and_never_the_platform_key() {
        use crate::ui::CliOutput;
        let out = super::ConsoleLoginOutput::Minted {
            code: "abc123".into(),
            expires_at: "2026-07-17T12:00:00Z".into(),
            session_id: "sid".into(),
            console_url: Some("http://localhost:28080/?api=1".into()),
            login: None,
        };
        let json = out.to_json().to_string();
        assert!(json.contains("abc123"), "the code is the point of the verb");
        assert!(
            !json.contains("agentos-dev-key"),
            "the platform key must never reach the output"
        );
    }

    /// A code minted against a plaintext NodePort console must name the loopback
    /// URL it can actually be spent at, as DATA: an agent reading `--json` needs
    /// `login.url`, not a sentence (#630, ADR-0049).
    #[test]
    fn console_login_carries_the_loginable_path_when_the_console_is_plaintext() {
        use crate::ui::CliOutput;
        let out = super::ConsoleLoginOutput::Minted {
            code: "abc123".into(),
            expires_at: "2026-07-17T12:00:00Z".into(),
            session_id: "sid".into(),
            console_url: Some("http://10.0.0.5:30080/?api=1".into()),
            login: crate::ops::login_path_for(
                "http://10.0.0.5:30080/?api=1",
                "agentos",
                "agentos-ui",
                80,
            ),
        };
        let json = out.to_json();
        assert_eq!(json["console_url"], "http://10.0.0.5:30080/?api=1");
        assert_eq!(json["login"]["url"], "http://localhost:8080/?api=1");
        assert_eq!(
            json["login"]["port_forward"],
            "kubectl -n agentos port-forward svc/agentos-ui 8080:80"
        );
    }

    #[test]
    fn shell_quote_escapes_an_embedded_single_quote() {
        // The one byte single-quoting cannot carry literally. Closing, escaping,
        // and reopening is what keeps the rest of the path inside the quotes.
        assert_eq!(super::shell_quote("/tmp/it's here"), r"'/tmp/it'\''s here'");
        assert_eq!(super::shell_quote("/tmp/plain"), "'/tmp/plain'");
    }

    #[tokio::test]
    async fn skill_approvals_clear_with_gate_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = super::skill_approvals(dir.path().to_path_buf(), vec!["X".into()], true)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_comma_in_gate_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        // A comma cannot round-trip through the CSV env encoding.
        let err = super::skill_approvals(dir.path().to_path_buf(), vec!["a,b".into()], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_whitespace_gate_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = super::skill_approvals(dir.path().to_path_buf(), vec!["  ".into()], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    // --- the set path must not be more credulous than the view path ----------
    // Both emit an answer ABOUT a specific bundle. The view path errors when the
    // bundle has no manifest; the set path emitted export-then-reboot guidance
    // naming a directory it had never opened, so `--plugin-dir /does/not/exist`
    // exited 0 and the guidance failed later at `skill up`.

    #[tokio::test]
    async fn skill_approvals_set_without_manifest_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = super::skill_approvals(dir.path().to_path_buf(), vec!["A".into()], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_clear_without_manifest_is_usage_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = super::skill_approvals(dir.path().to_path_buf(), vec![], true)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    #[tokio::test]
    async fn skill_approvals_set_with_valid_manifest_and_no_policy_succeeds() {
        use crate::ui::CliOutput;
        // Regression guard on the two-tier semantics: a manifest that parses but
        // declares no `approvalPolicy` is the legitimate no-gates case, not an
        // invalid bundle. Setting an env override for it must still work -- the
        // validation may only reject a missing, unreadable, or invalid manifest.
        let dir = tempfile::tempdir().unwrap();
        write_minimal_manifest(dir.path());
        let out = super::skill_approvals(dir.path().to_path_buf(), vec!["A".into()], false)
            .await
            .unwrap();
        assert_eq!(
            out.to_json()["env"].as_str().unwrap(),
            "AGENTOS_APPROVAL_REQUIRED_TOOLS=A"
        );
    }

    #[tokio::test]
    async fn skill_approvals_set_with_invalid_manifest_is_usage_error() {
        // The view path rejects a manifest the runner's parse would reject; the
        // set path names the same bundle, so it must reject it identically.
        let dir = tempfile::tempdir().unwrap();
        write_manifest(
            dir.path(),
            ".claude-plugin/plugin.json",
            r#"{"name":"x",,}"#,
        );
        let err = super::skill_approvals(dir.path().to_path_buf(), vec!["A".into()], false)
            .await
            .unwrap_err();
        assert_eq!(usage_class(&err), crate::exit::ExitClass::Usage);
    }

    /// AC2: an unavailable verb must name the concept's absence AND point at the
    /// tier that answers it. `main`'s human path renders `{err:#}` and discards
    /// the fix, so both halves have to survive on the Display surface alone.
    #[test]
    fn skill_versions_unavailable_message_names_reason_and_alternative() {
        let shown = format!("{:#}", super::skill_versions_unavailable());
        assert!(
            shown.contains(super::VERSIONS_REASON),
            "the human message must carry the reason: {shown}"
        );
        assert!(
            shown.contains(super::VERSIONS_ALT),
            "the human message must carry the cross-tier redirect: {shown}"
        );
    }

    #[test]
    fn skill_memory_unavailable_message_names_reason_and_alternative() {
        let shown = format!("{:#}", super::skill_memory_unavailable());
        assert!(
            shown.contains(super::MEMORY_REASON),
            "the human message must carry the reason: {shown}"
        );
        assert!(
            shown.contains(super::MEMORY_ALT),
            "the human message must carry the cross-tier redirect: {shown}"
        );
    }

    #[test]
    fn skill_versions_unavailable_is_unsupported() {
        let err = super::skill_versions_unavailable();
        assert_eq!(
            crate::exit::classify(&err).0,
            crate::exit::ExitClass::Unsupported
        );
        let json = crate::exit::error_json(&err);
        assert!(
            json["error"].as_str().unwrap().contains("versions"),
            "error names the concept: {}",
            json["error"]
        );
        let fix = json["fix"].as_str().unwrap();
        assert!(
            fix.contains("cluster") || fix.contains("local"),
            "fix names a cross-tier alternative: {fix}"
        );
    }

    #[test]
    fn skill_memory_unavailable_is_unsupported() {
        let err = super::skill_memory_unavailable();
        assert_eq!(
            crate::exit::classify(&err).0,
            crate::exit::ExitClass::Unsupported
        );
        let json = crate::exit::error_json(&err);
        assert!(
            json["error"].as_str().unwrap().contains("memory"),
            "error names the concept: {}",
            json["error"]
        );
        let fix = json["fix"].as_str().unwrap();
        assert!(
            fix.contains("cluster") || fix.contains("local"),
            "fix names a cross-tier alternative: {fix}"
        );
    }
}
