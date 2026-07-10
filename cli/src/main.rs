//! The `agentos` binary: `init`, `skill <up|down|status|message|eval>` for a
//! local runner, `local <up|down|status|message|deploy>` for the compose stack,
//! and `cluster <up|down|status|comms|message|deploy>` for Kubernetes and the
//! platform API. Task I1; contracts are frozen in packages/aci-protocol and
//! packages/plugin-format.

use std::path::PathBuf;

use agentos::artifacts;
use agentos::commands::{
    self, AgentActionOpts, DeployEnv, DeployOpts, SendType, StartOpts, DEFAULT_PORT,
};
use agentos::comms::{self, CommsOpts, LocalCommsOpts};
use agentos::local::{self, LocalDownOpts, LocalOpts};
use agentos::message::{self, MessageOpts};
use agentos::ops::{self, CommonOpts, DownOpts, UpOpts};
use agentos::state::{apply_continue, load_turn, CliTurnArgs, TurnVerb};
use agentos::ui::{self, ColorFlag, Ui};
use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "agentos",
    version,
    about = "AgentOS CLI: run a plugin locally, no Slack workspace needed"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
    /// Show verbose plumbing (helm/kubectl/rollout/port-forward).
    #[arg(
        long,
        global = true,
        help = "Show verbose plumbing (helm/kubectl/rollout/port-forward)"
    )]
    debug: bool,
    /// Payload only; suppress progress and diagnostics.
    #[arg(
        short = 'q',
        long,
        global = true,
        help = "Payload only; suppress progress and diagnostics"
    )]
    quiet: bool,
    /// Colorize output.
    #[arg(
        long,
        global = true,
        value_enum,
        default_value_t = ColorFlag::Auto,
        help = "Colorize output"
    )]
    color: ColorFlag,
}

#[derive(Subcommand)]
enum Command {
    /// Scaffold a new plugin bundle (Claude Code plugin shape).
    Init {
        /// Kebab-case plugin name (e.g. deal-desk).
        name: String,
        /// Target directory; defaults to ./<name>.
        #[arg(long)]
        dir: Option<PathBuf>,
    },
    /// Work with a local runner session for a plugin bundle.
    Skill {
        #[command(subcommand)]
        action: SkillAction,
    },
    /// Work with the local compose stack and local platform API.
    Local {
        #[command(subcommand)]
        action: LocalAction,
    },
    /// Work with the deployed cluster release and platform API.
    Cluster {
        #[command(subcommand)]
        action: ClusterAction,
    },
}

#[derive(Subcommand)]
enum SkillAction {
    /// Boot a local runner container for the bundle and print the env summary.
    Up {
        /// Plugin bundle directory.
        #[arg(long, default_value = ".")]
        plugin_dir: PathBuf,
        /// Runner image. Default: version-pinned `ghcr.io/curie-eng/agentos-runner:<version>` on release builds; local `agentos-runner` on dev builds. Pass to override.
        #[arg(long)]
        image: Option<String>,
        /// Host port for the local bot.
        #[arg(long, default_value_t = DEFAULT_PORT)]
        port: u16,
        /// Container name.
        #[arg(long, default_value = "agentos-runner-local")]
        name: String,
        /// Use the runner's scripted fake model (offline; no credential).
        #[arg(long)]
        fake_model: bool,
        /// Docker network to join (e.g. agentos_default for the dev stack).
        #[arg(long)]
        network: Option<String>,
        /// OTLP endpoint for traces (e.g. http://otel-collector:4318).
        #[arg(long)]
        otel_endpoint: Option<String>,
        /// ACI budget JSON for the session.
        #[arg(long, default_value = commands::DEFAULT_BUDGET)]
        budget: String,
        /// Model id, forwarded as AGENTOS_MODEL. Omit for the SDK default.
        /// Setting it makes token usage attributable in Langfuse traces.
        #[arg(long)]
        model: Option<String>,
        /// Run the named model through local Ollama.
        #[arg(
            long,
            num_args = 0..=1,
            default_missing_value = commands::DEFAULT_LOCAL_MODEL,
            conflicts_with = "fake_model",
            conflicts_with = "model"
        )]
        local_model: Option<String>,
    },
    /// Stop and remove the local runner container.
    Down,
    /// Show the local runner's session status.
    Status {
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
    },
    /// Send a synthetic event to the local runner and stream the reply.
    Message {
        /// The message text.
        text: String,
        /// Synthetic Slack user id.
        #[arg(long, default_value = "U-local")]
        user: String,
        /// ACI event type.
        #[arg(long, value_enum, default_value_t = SendType::Message)]
        event_type: SendType,
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
    },
    /// Run the bundle's eval cases through the local runner.
    Eval {
        /// Eval case file (default: evals/cases.json here, then the running
        /// bundle's).
        #[arg(long)]
        cases: Option<PathBuf>,
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
    },
}

/// Subcommands of `agentos local`.
#[derive(Subcommand)]
enum LocalAction {
    /// Bring the dev stack up (`core` with `--minimal`, else `full`) and print URLs. Add `--slack` for the optional dispatcher.
    Up {
        /// Compose file. Default: version-pinned `compose.release.yaml` from the remote on release builds; local `compose.dev.yaml` on dev builds. Pass to override.
        #[arg(short = 'f', long)]
        file: Option<String>,
        /// Print the docker compose command and exit without executing.
        #[arg(long)]
        dry_run: bool,
        /// Bring up only the 7 core services (skip Langfuse/ClickHouse/OTel/UI).
        #[arg(long)]
        minimal: bool,
        /// Run the named model through local Ollama.
        #[arg(
            long,
            num_args = 0..=1,
            default_missing_value = commands::DEFAULT_LOCAL_MODEL
        )]
        local_model: Option<String>,
        /// Also start the optional Slack dispatcher (adds --profile slack).
        #[arg(long)]
        slack: bool,
    },
    /// Stop the dev stack (docker compose down), keeping volumes.
    Down {
        /// Compose file. Default: version-pinned `compose.release.yaml` from the remote on release builds; local `compose.dev.yaml` on dev builds. Pass to override.
        #[arg(short = 'f', long)]
        file: Option<String>,
        /// Also destroy volumes (adds -v). Prompts for confirmation unless --yes.
        #[arg(long)]
        wipe: bool,
        /// Skip the --wipe confirmation prompt.
        #[arg(long)]
        yes: bool,
        /// Print the docker compose command and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Show the dev stack's service status (docker compose ps).
    Status {
        /// Compose file. Default: version-pinned `compose.release.yaml` from the remote on release builds; local `compose.dev.yaml` on dev builds. Pass to override.
        #[arg(short = 'f', long)]
        file: Option<String>,
        /// Print the docker compose command and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Connect or disconnect the local compose stack from a real Slack workspace.
    Comms {
        /// Chat surface to configure. Required until the CLI grows more than
        /// one comms target.
        #[arg(long)]
        slack: bool,
        /// Clear Slack from the local stack instead of connecting it.
        #[arg(long)]
        disconnect: bool,
        /// Slack app token. Defaults from SLACK_APP_TOKEN.
        #[arg(
            long,
            env = "SLACK_APP_TOKEN",
            hide_env_values = true,
            default_value = ""
        )]
        app_token: String,
        /// Slack bot token. Defaults from SLACK_BOT_TOKEN.
        #[arg(
            long,
            env = "SLACK_BOT_TOKEN",
            hide_env_values = true,
            default_value = ""
        )]
        bot_token: String,
        /// Compose file. Default: version-pinned `compose.release.yaml` from the remote on release builds; local `compose.dev.yaml` on dev builds. Pass to override.
        #[arg(short = 'f', long)]
        file: Option<String>,
        /// Print the docker compose command(s) that would run and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Drive the local compose stack end to end with zero Slack contact.
    Message {
        /// The user message text.
        text: String,
        /// Slack channel id to send as; must match the target agent's
        /// slack_channel. Omit to use the sole deployed agent's channel (errors
        /// if zero or multiple agents are deployed).
        #[arg(long)]
        channel: Option<String>,
        /// Existing thread ts to continue a conversation; omit to start a new
        /// thread. Pair with --channel to keep multi-turn context.
        #[arg(long)]
        thread: Option<String>,
        /// Reuse the last turn's context (channel, thread, transport) recorded
        /// in .agentos/last-turn.json in the working directory; type only the
        /// new message text.
        #[arg(long = "continue")]
        r#continue: bool,
        /// Valkey password (compose default `valkeypass`). Prefer the
        /// AGENTOS_VALKEY_PASSWORD env var over passing a real secret on the
        /// command line, where it leaks via `ps` and shell history.
        #[arg(
            long,
            env = "AGENTOS_VALKEY_PASSWORD",
            hide_env_values = true,
            default_value = message::DEFAULT_VALKEY_PASSWORD
        )]
        valkey_password: String,
        /// Local mode only: platform API base URL for the channel lookup.
        #[arg(long)]
        api_url: Option<String>,
        /// Platform API key for the default-channel lookup.
        #[arg(long, env = "AGENTOS_API_KEY", default_value = message::DEFAULT_API_KEY)]
        api_key: String,
        /// Synthetic Slack user id for the enqueued event.
        #[arg(long, default_value = message::DEFAULT_USER)]
        user: String,
        /// Stream the dispatcher enqueues onto.
        #[arg(long, env = "AGENTOS_STREAM", default_value = message::DEFAULT_STREAM)]
        stream: String,
        /// How long to wait for the worker's reply before printing diagnostics.
        /// Default: 300 seconds.
        #[arg(long)]
        timeout_secs: Option<u64>,
        /// Print the queue and stub plan that a real run would produce, and exit.
        #[arg(long)]
        dry_run: bool,
    },
    /// Push the bundle to the local platform API and deploy it.
    Deploy {
        /// Plugin bundle directory.
        #[arg(long, default_value = ".")]
        plugin_dir: PathBuf,
        /// Platform API base URL.
        #[arg(
            long,
            default_value = message::DEFAULT_LOCAL_API_URL,
            env = "AGENTOS_API_URL"
        )]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Slack channel to bind the agent to. On first create it defaults to
        /// #local-dev; on redeploy it is only moved when you pass this flag, so
        /// omitting it leaves the deployed agent's channel untouched.
        #[arg(long)]
        slack_channel: Option<String>,
        /// Target environment.
        #[arg(long, value_enum, default_value_t = DeployEnv::Dev)]
        env: DeployEnv,
        /// Version label; defaults to <manifest version>-<unix time>.
        #[arg(long)]
        label: Option<String>,
    },
}

#[derive(Subcommand)]
enum ClusterAction {
    /// Install or upgrade the AgentOS release via Helm (helm upgrade --install).
    /// By default it puts the UI and Langfuse on node ports for tailnet/LAN
    /// access; pass --no-expose to keep them ClusterIP-only. Set
    /// AGENTOS_MODEL_CREDENTIALS (an Anthropic API key) to install with the real
    /// model and egress opened to the provider; without it the install is sealed
    /// (fake model, canned replies) and re-running with the env var set goes live.
    Up {
        /// Kubernetes namespace.
        #[arg(long, default_value = "agentos")]
        namespace: String,
        /// Helm release name.
        #[arg(long, default_value = "agentos")]
        release: String,
        /// Helm chart. Default: the version-pinned chart release asset on release builds; local `charts/agentos` on dev builds. Pass a path or ref to override.
        #[arg(long)]
        chart: Option<String>,
        /// Keep the UI and Langfuse services ClusterIP instead of NodePort.
        #[arg(long)]
        no_expose: bool,
        /// Force the sealed fake-model install even when AGENTOS_MODEL_CREDENTIALS
        /// is set (dev/CI escape hatch); suppresses the fake-model warning.
        #[arg(long)]
        fake_model: bool,
        /// Run the named model through the chart inference deployment.
        #[arg(
            long,
            num_args = 0..=1,
            default_missing_value = commands::DEFAULT_LOCAL_MODEL,
            conflicts_with = "fake_model"
        )]
        local_model: Option<String>,
        /// Open runner egress to a declared destination for skill web access,
        /// repeatable CIDR, TCP 443. Additive to the model egress; omit to stay
        /// fully sealed.
        #[arg(long = "allow-web-egress", value_name = "CIDR")]
        allow_web_egress: Vec<String>,
        /// Extra `--set KEY=VAL` passed through to helm verbatim (repeatable).
        #[arg(long = "set", value_name = "KEY=VAL")]
        set: Vec<String>,
        /// Install with the chart's built-in dev-default secrets instead of
        /// generating strong per-release randoms. Deterministic, for local dev
        /// and CI only -- these defaults are published in the public repo.
        #[arg(long)]
        dev: bool,
        /// Print the helm command that would run and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Uninstall the release and sweep its runtime namespaces (helm uninstall +
    /// kubectl delete namespace). The agents.x-k8s.io CRDs are left in place.
    Down {
        /// Kubernetes namespace.
        #[arg(long, default_value = "agentos")]
        namespace: String,
        /// Helm release name.
        #[arg(long, default_value = "agentos")]
        release: String,
        /// Skip the interactive confirmation prompt.
        #[arg(long)]
        yes: bool,
        /// Print the commands that would run and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Report release health and access URLs (read-only: helm status + kubectl).
    Status {
        /// Kubernetes namespace.
        #[arg(long, default_value = "agentos")]
        namespace: String,
        /// Helm release name.
        #[arg(long, default_value = "agentos")]
        release: String,
        /// Print the read-only commands that would run and exit.
        #[arg(long)]
        dry_run: bool,
    },
    /// Connect or disconnect the cluster release from a real Slack workspace.
    Comms {
        /// Chat surface to configure. Required until the CLI grows more than
        /// one comms target.
        #[arg(long)]
        slack: bool,
        /// Clear the Slack tokens from the release instead of setting them.
        #[arg(long)]
        disconnect: bool,
        /// Slack app token. Defaults from SLACK_APP_TOKEN.
        #[arg(
            long,
            env = "SLACK_APP_TOKEN",
            hide_env_values = true,
            default_value = ""
        )]
        app_token: String,
        /// Slack bot token. Defaults from SLACK_BOT_TOKEN.
        #[arg(
            long,
            env = "SLACK_BOT_TOKEN",
            hide_env_values = true,
            default_value = ""
        )]
        bot_token: String,
        /// Kubernetes namespace.
        #[arg(long, default_value = "agentos")]
        namespace: String,
        /// Helm release name.
        #[arg(long, default_value = "agentos")]
        release: String,
        /// Helm chart. Default: the version-pinned chart release asset on release builds; local `charts/agentos` on dev builds. Pass a path or ref to override.
        #[arg(long)]
        chart: Option<String>,
        /// Print the helm command that would run and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Drive the deployed Kubernetes release end to end with zero Slack contact.
    Message {
        /// The user message text.
        text: String,
        /// Slack channel id to send as; must match the target agent's
        /// slack_channel. Omit to use the sole deployed agent's channel (errors
        /// if zero or multiple agents are deployed).
        #[arg(long)]
        channel: Option<String>,
        /// Existing thread ts to continue a conversation; omit to start a new
        /// thread. Pair with --channel to keep multi-turn context.
        #[arg(long)]
        thread: Option<String>,
        /// Reuse the last turn's context (channel, thread, transport) recorded
        /// in .agentos/last-turn.json in the working directory; type only the
        /// new message text.
        #[arg(long = "continue")]
        r#continue: bool,
        /// Kubernetes namespace of the release. Default: agentos.
        #[arg(long)]
        namespace: Option<String>,
        /// Helm release name. Default: agentos.
        #[arg(long)]
        release: Option<String>,
        /// Helm chart. Default: the version-pinned chart release asset on release builds; local `charts/agentos` on dev builds. Pass a path or ref to override.
        #[arg(long)]
        chart: Option<String>,
        /// Host the in-cluster worker uses to reach the stub. Omit to auto-detect
        /// the local IP the kernel would use to reach the cluster.
        #[arg(long)]
        listen_host: Option<String>,
        /// Port the stub binds (0.0.0.0); the worker posts here.
        #[arg(long, default_value_t = message::DEFAULT_LISTEN_PORT)]
        listen_port: u16,
        /// Local port the Valkey port-forward binds.
        #[arg(long, default_value_t = message::DEFAULT_VALKEY_LOCAL_PORT)]
        valkey_local_port: u16,
        /// Valkey password (chart default `valkeypass`). Prefer the
        /// AGENTOS_VALKEY_PASSWORD env var over passing a real secret on the
        /// command line, where it leaks via `ps` and shell history.
        #[arg(
            long,
            env = "AGENTOS_VALKEY_PASSWORD",
            hide_env_values = true,
            default_value = message::DEFAULT_VALKEY_PASSWORD
        )]
        valkey_password: String,
        /// Local port the API port-forward binds (default-channel lookup).
        #[arg(long, default_value_t = message::DEFAULT_API_LOCAL_PORT)]
        api_local_port: u16,
        /// Platform API key for the default-channel lookup.
        #[arg(long, env = "AGENTOS_API_KEY", default_value = message::DEFAULT_API_KEY)]
        api_key: String,
        /// Synthetic Slack user id for the enqueued event.
        #[arg(long, default_value = message::DEFAULT_USER)]
        user: String,
        /// Stream the dispatcher enqueues onto.
        #[arg(long, env = "AGENTOS_STREAM", default_value = message::DEFAULT_STREAM)]
        stream: String,
        /// How long to wait for the worker's reply before printing diagnostics.
        /// Defaults high because the worker kernel can retry a run up to 3 times
        /// with a 90s sandbox-claim timeout each (worst case near 270s of claim
        /// waits alone), so a shorter ceiling can time out while it is still working.
        /// Default: 300 seconds.
        #[arg(long)]
        timeout_secs: Option<u64>,
        /// Skip pointing the worker at the stub. Wiring is on by default (helm
        /// upgrade + rollout wait); with --no-wire the command refuses to run
        /// unless the worker is already wired, printing the exact command to run.
        #[arg(long = "no-wire")]
        no_wire: bool,
        /// Wire even when the release is connected to a real Slack workspace
        /// (a dispatcher deployment exists). Without this the wiring is refused,
        /// since the stub would hijack that workspace's replies cluster-wide.
        #[arg(long)]
        force_wire: bool,
        /// Print the kubectl/helm commands, stub URL, and enqueue description that
        /// a real run would produce, and exit without executing anything.
        #[arg(long)]
        dry_run: bool,
    },
    /// Push the bundle to the platform API and deploy it.
    Deploy {
        /// Plugin bundle directory.
        #[arg(long, default_value = ".")]
        plugin_dir: PathBuf,
        /// Platform API base URL.
        #[arg(long, default_value = "http://localhost:8000", env = "AGENTOS_API_URL")]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Slack channel to bind the agent to. On first create it defaults to
        /// #local-dev; on redeploy it is only moved when you pass this flag, so
        /// omitting it leaves the deployed agent's channel untouched.
        #[arg(long)]
        slack_channel: Option<String>,
        /// Target environment.
        #[arg(long, value_enum, default_value_t = DeployEnv::Dev)]
        env: DeployEnv,
        /// Version label; defaults to <manifest version>-<unix time>.
        #[arg(long)]
        label: Option<String>,
    },
    // Agent-lifecycle verbs (kill/resume/budget/delete) speak the platform API
    // like `deploy` does. Design decision (#149): extend the existing `cluster`
    // target rather than introduce a new top-level `agent` noun -- these act on a
    // deployed release's agents, so they belong beside `cluster deploy`/`message`
    // and reuse its `--api-url`/`--api-key` surface and agent resolution.
    /// Kill an agent (stop its runs) via the platform API (`POST /agents/{id}/kill`).
    Kill {
        /// Agent name or id to kill.
        agent: String,
        /// Platform API base URL.
        #[arg(long, default_value = "http://localhost:8000", env = "AGENTOS_API_URL")]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Confirm this destructive action (required; it stops the agent's runs).
        #[arg(long)]
        yes: bool,
        /// Print what would be done and exit without making a request.
        #[arg(long)]
        dry_run: bool,
    },
    /// Resume a killed agent via the platform API (`POST /agents/{id}/resume`).
    Resume {
        /// Agent name or id to resume.
        agent: String,
        /// Platform API base URL.
        #[arg(long, default_value = "http://localhost:8000", env = "AGENTOS_API_URL")]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Print what would be done and exit without making a request.
        #[arg(long)]
        dry_run: bool,
    },
    /// Set an agent's budget via the platform API (`PUT /agents/{id}/budget`).
    Budget {
        /// Agent name or id.
        agent: String,
        /// Daily spend cap in USD (BudgetConfig.max_usd_per_day). Must be > 0.
        #[arg(long)]
        limit: f64,
        /// Platform API base URL.
        #[arg(long, default_value = "http://localhost:8000", env = "AGENTOS_API_URL")]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Print what would be done and exit without making a request.
        #[arg(long)]
        dry_run: bool,
    },
    /// Delete an agent via the platform API (`DELETE /agents/{id}`).
    Delete {
        /// Agent name or id to delete.
        agent: String,
        /// Platform API base URL.
        #[arg(long, default_value = "http://localhost:8000", env = "AGENTOS_API_URL")]
        api_url: String,
        /// Platform API key.
        #[arg(long, default_value = "agentos-dev-key", env = "AGENTOS_API_KEY")]
        api_key: String,
        /// Confirm this destructive action (required; it permanently deletes the agent).
        #[arg(long)]
        yes: bool,
        /// Print what would be done and exit without making a request.
        #[arg(long)]
        dry_run: bool,
    },
}

async fn resolve_compose_file(file: Option<String>, dry_run: bool) -> Result<String> {
    let resolved = artifacts::resolve_compose(
        file.as_deref(),
        artifacts::Channel::current(),
        artifacts::version(),
        artifacts::cache_root,
        std::path::Path::new(local::DEFAULT_COMPOSE_FILE).exists(),
    )?;
    materialize_artifact(resolved, dry_run, "compose").await
}

async fn materialize_artifact(
    resolved: artifacts::Resolved,
    dry_run: bool,
    label: &str,
) -> Result<String> {
    if dry_run {
        if let artifacts::Resolved::Fetch { url, .. } = &resolved {
            ui::ui().note(&format!("{label} source: {}", ui::ui().url(url)));
        }
        Ok(resolved.planned_target().display().to_string())
    } else {
        Ok(artifacts::ensure_cached(&resolved)
            .await?
            .display()
            .to_string())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Some(hint) = agentos::retired_hint(&args) {
        eprintln!("{hint}");
        std::process::exit(2);
    }

    let cli = Cli::parse();
    ui::init(Ui::from_process(cli.color, cli.debug, cli.quiet));
    match cli.command {
        Command::Init { name, dir } => commands::init(&name, dir),
        Command::Skill { action } => match action {
            SkillAction::Up {
                plugin_dir,
                image,
                port,
                name,
                fake_model,
                network,
                otel_endpoint,
                budget,
                model,
                local_model,
            } => {
                let image = artifacts::resolve_image(
                    image.as_deref(),
                    artifacts::Channel::current(),
                    artifacts::version(),
                );
                commands::start(StartOpts {
                    plugin_dir,
                    image,
                    port,
                    name,
                    fake_model,
                    network,
                    otel_endpoint,
                    budget,
                    model,
                    local_model,
                })
                .await
            }
            SkillAction::Down => commands::stop().await,
            SkillAction::Status { url } => commands::status(url).await,
            SkillAction::Message {
                text,
                user,
                event_type,
                url,
            } => commands::send(&text, &user, event_type.into(), url).await,
            SkillAction::Eval { cases, url } => commands::eval(cases, url).await,
        },
        Command::Local { action } => match action {
            LocalAction::Up {
                file,
                dry_run,
                minimal,
                local_model,
                slack,
            } => {
                let file = resolve_compose_file(file, dry_run).await?;
                local::up(LocalOpts {
                    file,
                    dry_run,
                    minimal,
                    local_model,
                    slack,
                })
                .await
            }
            LocalAction::Down {
                file,
                wipe,
                yes,
                dry_run,
            } => {
                let file = resolve_compose_file(file, dry_run).await?;
                local::down(LocalDownOpts {
                    common: LocalOpts {
                        file,
                        dry_run,
                        minimal: false,
                        local_model: None,
                        slack: false,
                    },
                    wipe,
                    yes,
                })
                .await
            }
            LocalAction::Status { file, dry_run } => {
                let file = resolve_compose_file(file, dry_run).await?;
                local::status(LocalOpts {
                    file,
                    dry_run,
                    minimal: false,
                    local_model: None,
                    slack: false,
                })
                .await
            }
            LocalAction::Comms {
                slack,
                disconnect,
                app_token,
                bot_token,
                file,
                dry_run,
            } => {
                comms::require_provider(slack)?;
                let resolved_file = resolve_compose_file(file, dry_run).await?;
                comms::local_comms(LocalCommsOpts {
                    file: resolved_file,
                    dry_run,
                    app_token,
                    bot_token,
                    disconnect,
                })
                .await
            }
            LocalAction::Message {
                text,
                channel,
                thread,
                r#continue,
                valkey_password,
                api_url,
                api_key,
                user,
                stream,
                timeout_secs,
                dry_run,
            } => {
                let state = if r#continue {
                    match load_turn(&std::env::current_dir()?)? {
                        Some(state) => Some(state),
                        None => anyhow::bail!(
                            "no previous turn recorded in .agentos/last-turn.json; run a message without --continue first"
                        ),
                    }
                } else {
                    None
                };
                let resolved = apply_continue(
                    TurnVerb::Local,
                    CliTurnArgs {
                        channel,
                        thread,
                        namespace: None,
                        release: None,
                        chart: None,
                        listen_host: None,
                        timeout_secs,
                        api_url,
                        api_key,
                    },
                    state,
                    std::env::var("AGENTOS_API_KEY").ok(),
                )?;
                message::message(MessageOpts {
                    text,
                    channel: resolved.channel,
                    thread: resolved.thread,
                    namespace: "agentos".into(),
                    release: "agentos".into(),
                    chart: "charts/agentos".into(),
                    listen_host: None,
                    listen_port: message::DEFAULT_LISTEN_PORT,
                    valkey_local_port: message::DEFAULT_VALKEY_LOCAL_PORT,
                    valkey_password,
                    api_local_port: message::DEFAULT_API_LOCAL_PORT,
                    api_key: resolved.api_key,
                    user,
                    stream,
                    timeout_secs: resolved.timeout_secs,
                    wire: true,
                    force_wire: false,
                    dry_run,
                    local: true,
                    api_url: resolved.api_url,
                })
                .await
            }
            LocalAction::Deploy {
                plugin_dir,
                api_url,
                api_key,
                slack_channel,
                env,
                label,
            } => {
                commands::deploy(DeployOpts {
                    plugin_dir,
                    api_url,
                    api_key,
                    slack_channel,
                    env,
                    label,
                })
                .await
            }
        },
        Command::Cluster { action } => match action {
            ClusterAction::Up {
                namespace,
                release,
                chart,
                no_expose,
                fake_model,
                local_model,
                allow_web_egress,
                set,
                dev,
                dry_run,
            } => {
                let resolved = artifacts::resolve_chart(
                    chart.as_deref(),
                    artifacts::Channel::current(),
                    artifacts::version(),
                    artifacts::cache_root,
                    std::path::Path::new("charts/agentos").is_dir(),
                )?;
                let chart = materialize_artifact(resolved, dry_run, "chart").await?;
                let credentials = if local_model.is_some() {
                    None
                } else {
                    ops::resolve_up_credentials(
                        fake_model,
                        std::env::var("AGENTOS_MODEL_CREDENTIALS").ok(),
                    )
                };
                ops::up(UpOpts {
                    common: CommonOpts {
                        namespace,
                        release,
                        dry_run,
                    },
                    chart,
                    no_expose,
                    set,
                    allow_web_egress,
                    fake_model,
                    credentials,
                    local_model,
                    // Populated by ops::up (generate on fresh install / reuse on
                    // upgrade); empty here so the pure builder starts clean.
                    secrets: vec![],
                    dev,
                })
                .await
            }
            ClusterAction::Down {
                namespace,
                release,
                yes,
                dry_run,
            } => {
                ops::down(DownOpts {
                    common: CommonOpts {
                        namespace,
                        release,
                        dry_run,
                    },
                    yes,
                })
                .await
            }
            ClusterAction::Status {
                namespace,
                release,
                dry_run,
            } => {
                ops::status(CommonOpts {
                    namespace,
                    release,
                    dry_run,
                })
                .await
            }
            ClusterAction::Comms {
                slack,
                disconnect,
                app_token,
                bot_token,
                namespace,
                release,
                chart,
                dry_run,
            } => {
                comms::require_provider(slack)?;
                let resolved = artifacts::resolve_chart(
                    chart.as_deref(),
                    artifacts::Channel::current(),
                    artifacts::version(),
                    artifacts::cache_root,
                    std::path::Path::new("charts/agentos").is_dir(),
                )?;
                let chart = materialize_artifact(resolved, dry_run, "chart").await?;
                comms::comms(CommsOpts {
                    common: CommonOpts {
                        namespace,
                        release,
                        dry_run,
                    },
                    chart,
                    app_token,
                    bot_token,
                    disconnect,
                })
                .await
            }
            ClusterAction::Message {
                text,
                channel,
                thread,
                r#continue,
                namespace,
                release,
                chart,
                listen_host,
                listen_port,
                valkey_local_port,
                valkey_password,
                api_local_port,
                api_key,
                user,
                stream,
                timeout_secs,
                no_wire,
                force_wire,
                dry_run,
            } => {
                let state = if r#continue {
                    match load_turn(&std::env::current_dir()?)? {
                        Some(state) => Some(state),
                        None => anyhow::bail!(
                            "no previous turn recorded in .agentos/last-turn.json; run a message without --continue first"
                        ),
                    }
                } else {
                    None
                };
                let resolved = apply_continue(
                    TurnVerb::Cluster,
                    CliTurnArgs {
                        channel,
                        thread,
                        namespace,
                        release,
                        chart,
                        listen_host,
                        timeout_secs,
                        api_url: None,
                        api_key,
                    },
                    state,
                    std::env::var("AGENTOS_API_KEY").ok(),
                )?;
                let resolved_chart = artifacts::resolve_chart(
                    resolved.chart.as_deref(),
                    artifacts::Channel::current(),
                    artifacts::version(),
                    artifacts::cache_root,
                    std::path::Path::new("charts/agentos").is_dir(),
                )?;
                let chart = materialize_artifact(resolved_chart, dry_run, "chart").await?;
                message::message(MessageOpts {
                    text,
                    channel: resolved.channel,
                    thread: resolved.thread,
                    namespace: resolved.namespace,
                    release: resolved.release,
                    chart,
                    listen_host: resolved.listen_host,
                    listen_port,
                    valkey_local_port,
                    valkey_password,
                    api_local_port,
                    api_key: resolved.api_key,
                    user,
                    stream,
                    timeout_secs: resolved.timeout_secs,
                    wire: !no_wire,
                    force_wire,
                    dry_run,
                    local: false,
                    api_url: None,
                })
                .await
            }
            ClusterAction::Deploy {
                plugin_dir,
                api_url,
                api_key,
                slack_channel,
                env,
                label,
            } => {
                commands::deploy(DeployOpts {
                    plugin_dir,
                    api_url,
                    api_key,
                    slack_channel,
                    env,
                    label,
                })
                .await
            }
            ClusterAction::Kill {
                agent,
                api_url,
                api_key,
                yes,
                dry_run,
            } => {
                commands::kill(
                    AgentActionOpts {
                        api_url,
                        api_key,
                        agent,
                        dry_run,
                    },
                    yes,
                )
                .await
            }
            ClusterAction::Resume {
                agent,
                api_url,
                api_key,
                dry_run,
            } => {
                commands::resume(AgentActionOpts {
                    api_url,
                    api_key,
                    agent,
                    dry_run,
                })
                .await
            }
            ClusterAction::Budget {
                agent,
                limit,
                api_url,
                api_key,
                dry_run,
            } => {
                commands::budget(
                    AgentActionOpts {
                        api_url,
                        api_key,
                        agent,
                        dry_run,
                    },
                    limit,
                )
                .await
            }
            ClusterAction::Delete {
                agent,
                api_url,
                api_key,
                yes,
                dry_run,
            } => {
                commands::delete(
                    AgentActionOpts {
                        api_url,
                        api_key,
                        agent,
                        dry_run,
                    },
                    yes,
                )
                .await
            }
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn clap_surface_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn local_message_accepts_api_key() {
        let cli = Cli::try_parse_from(["agentos", "local", "message", "--api-key", "K", "hi"])
            .expect("local message --api-key should parse");
        match cli.command {
            Command::Local {
                action: LocalAction::Message { api_key, .. },
            } => assert_eq!(api_key, "K"),
            _ => panic!("expected local message command"),
        }
    }

    #[test]
    fn local_short_file_flag_parses_for_all_verbs() {
        let cases = [
            (["agentos", "local", "up", "-f", "custom.yaml"], "up"),
            (["agentos", "local", "down", "-f", "custom.yaml"], "down"),
            (
                ["agentos", "local", "status", "-f", "custom.yaml"],
                "status",
            ),
        ];

        for (argv, verb) in cases {
            let cli = Cli::try_parse_from(argv).expect("local verb accepts -f");
            match cli.command {
                Command::Local {
                    action: LocalAction::Up { file, .. },
                } => {
                    assert_eq!(verb, "up");
                    assert_eq!(file.as_deref(), Some("custom.yaml"));
                }
                Command::Local {
                    action: LocalAction::Down { file, .. },
                } => {
                    assert_eq!(verb, "down");
                    assert_eq!(file.as_deref(), Some("custom.yaml"));
                }
                Command::Local {
                    action: LocalAction::Status { file, .. },
                } => {
                    assert_eq!(verb, "status");
                    assert_eq!(file.as_deref(), Some("custom.yaml"));
                }
                _ => panic!("expected the local subcommand"),
            }
        }
    }

    #[test]
    fn local_up_parses_minimal_flag() {
        let cli = Cli::try_parse_from(["agentos", "local", "up", "--minimal"])
            .expect("local up --minimal should parse");
        match cli.command {
            Command::Local {
                action: LocalAction::Up { minimal, .. },
            } => assert!(minimal),
            _ => panic!("expected local up command"),
        }
    }

    #[test]
    fn local_up_parses_slack_flag() {
        let cli = Cli::try_parse_from(["agentos", "local", "up", "--slack"])
            .expect("local up --slack should parse");
        match cli.command {
            Command::Local {
                action: LocalAction::Up { slack, .. },
            } => assert!(slack),
            _ => panic!("expected local up command"),
        }
    }

    #[test]
    fn local_comms_parses_slack_disconnect_and_app_token() {
        let cli = Cli::try_parse_from([
            "agentos",
            "local",
            "comms",
            "--slack",
            "--disconnect",
            "--app-token",
            "X",
        ])
        .expect("local comms flags should parse");
        match cli.command {
            Command::Local {
                action:
                    LocalAction::Comms {
                        slack,
                        disconnect,
                        app_token,
                        ..
                    },
            } => {
                assert!(slack);
                assert!(disconnect);
                assert_eq!(app_token, "X");
            }
            _ => panic!("expected local comms command"),
        }
    }

    #[test]
    fn cluster_kill_parses_agent_and_yes() {
        let cli = Cli::try_parse_from(["agentos", "cluster", "kill", "deal-desk", "--yes"])
            .expect("cluster kill should parse");
        match cli.command {
            Command::Cluster {
                action: ClusterAction::Kill { agent, yes, .. },
            } => {
                assert_eq!(agent, "deal-desk");
                assert!(yes);
            }
            _ => panic!("expected cluster kill command"),
        }
    }

    #[test]
    fn cluster_kill_defaults_yes_and_dry_run_off() {
        let cli = Cli::try_parse_from(["agentos", "cluster", "kill", "a"])
            .expect("cluster kill without flags should parse");
        match cli.command {
            Command::Cluster {
                action:
                    ClusterAction::Kill {
                        agent,
                        yes,
                        dry_run,
                        ..
                    },
            } => {
                assert_eq!(agent, "a");
                assert!(!yes);
                assert!(!dry_run);
            }
            _ => panic!("expected cluster kill command"),
        }
    }

    #[test]
    fn cluster_resume_parses_agent_and_dry_run() {
        let cli = Cli::try_parse_from(["agentos", "cluster", "resume", "a", "--dry-run"])
            .expect("cluster resume should parse");
        match cli.command {
            Command::Cluster {
                action: ClusterAction::Resume { agent, dry_run, .. },
            } => {
                assert_eq!(agent, "a");
                assert!(dry_run);
            }
            _ => panic!("expected cluster resume command"),
        }
    }

    #[test]
    fn cluster_budget_parses_agent_and_limit() {
        let cli = Cli::try_parse_from(["agentos", "cluster", "budget", "a", "--limit", "12.5"])
            .expect("cluster budget should parse");
        match cli.command {
            Command::Cluster {
                action: ClusterAction::Budget { agent, limit, .. },
            } => {
                assert_eq!(agent, "a");
                assert_eq!(limit, 12.5);
            }
            _ => panic!("expected cluster budget command"),
        }
    }

    #[test]
    fn cluster_budget_requires_limit() {
        // `--limit` has no default, so omitting it is a parse error (not a silent
        // zero-budget request).
        assert!(Cli::try_parse_from(["agentos", "cluster", "budget", "a"]).is_err());
    }

    #[test]
    fn cluster_delete_parses_agent_and_yes() {
        let cli = Cli::try_parse_from(["agentos", "cluster", "delete", "a", "--yes"])
            .expect("cluster delete should parse");
        match cli.command {
            Command::Cluster {
                action: ClusterAction::Delete { agent, yes, .. },
            } => {
                assert_eq!(agent, "a");
                assert!(yes);
            }
            _ => panic!("expected cluster delete command"),
        }
    }

    #[test]
    fn cluster_comms_parses_slack_disconnect_and_app_token() {
        let cli = Cli::try_parse_from([
            "agentos",
            "cluster",
            "comms",
            "--slack",
            "--disconnect",
            "--app-token",
            "X",
        ])
        .expect("cluster comms flags should parse");
        match cli.command {
            Command::Cluster {
                action:
                    ClusterAction::Comms {
                        slack,
                        disconnect,
                        app_token,
                        ..
                    },
            } => {
                assert!(slack);
                assert!(disconnect);
                assert_eq!(app_token, "X");
            }
            _ => panic!("expected cluster comms command"),
        }
    }
}
