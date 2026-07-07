//! The `agentos` binary: init/start/send/eval a plugin against a local runner,
//! stop/runner-status/steer/interrupt for the container lifecycle, chat to drive the
//! whole system with the CLI acting as the Slack service, message to drive a
//! deployed Kubernetes release the same way with zero Slack, and deploy for the
//! platform API. Task I1; contracts are frozen in packages/aci-protocol and
//! packages/plugin-format.

use std::path::PathBuf;

use agentos::chat::{self, ChatOpts};
use agentos::commands::{self, DeployEnv, DeployOpts, SendType, StartOpts, DEFAULT_PORT};
use agentos::local::{self, LocalDownOpts, LocalOpts};
use agentos::message::{self, MessageOpts};
use agentos::ops::{self, CommonOpts, DownOpts, UpOpts};
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
    /// Boot a local runner container for the bundle and print the env summary.
    Start {
        /// Plugin bundle directory.
        #[arg(long, default_value = ".")]
        plugin_dir: PathBuf,
        /// Runner image to boot.
        #[arg(long, default_value = "agentos-runner")]
        image: String,
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
    },
    /// Stop and remove the local runner container.
    Stop,
    /// Show the local runner's session status.
    #[command(name = "runner-status")]
    RunnerStatus {
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
    },
    /// Send a synthetic event to the local runner and stream the reply.
    Send {
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
    /// Drive the DEPLOYED Kubernetes release end to end with zero Slack contact.
    /// Self-plumbs kubectl port-forwards to the in-cluster Valkey and API, points
    /// the deployed worker at a local Slack Web API stub (helm upgrade
    /// --reuse-values), enqueues the dispatcher's event, and prints the worker's
    /// reply. Lets you exercise the full deployed machinery (queue -> worker ->
    /// sandbox -> real skill -> reply) without any Slack workspace or tokens. For
    /// the local compose-stack variant use `chat`.
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
        /// Kubernetes namespace of the release.
        #[arg(long, default_value = "agentos")]
        namespace: String,
        /// Helm release name.
        #[arg(long, default_value = "agentos")]
        release: String,
        /// Chart path for the wiring `helm upgrade` (run from the repo root for
        /// the default).
        #[arg(long, default_value = "charts/agentos")]
        chart: String,
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
        /// Valkey password (chart default `valkeypass`).
        #[arg(long, default_value = message::DEFAULT_VALKEY_PASSWORD)]
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
        #[arg(long, default_value_t = message::DEFAULT_TIMEOUT_SECS)]
        timeout_secs: u64,
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
        /// Drive the LOCAL compose stack (`agentos local up`) instead of a
        /// Kubernetes release: enqueue straight to the compose Valkey and let the
        /// containerized worker answer. No kubectl, helm, or port-forwards.
        /// Composes with --channel/--thread/--timeout-secs; rejects the
        /// cluster-only flags.
        #[arg(
            long,
            conflicts_with_all = [
                "namespace", "release", "chart", "listen_host", "listen_port",
                "valkey_local_port", "api_local_port", "no_wire", "force_wire",
            ]
        )]
        local: bool,
        /// Local mode only: platform API base URL for the channel lookup
        /// (default the compose API on http://localhost:28000).
        #[arg(long, requires = "local")]
        api_url: Option<String>,
    },
    /// Drive the whole system end to end with no Slack: the CLI runs a local
    /// Slack Web API stub, enqueues the dispatcher's event onto Valkey, and waits
    /// for the worker to finalize the turn at the stub. Run the worker with
    /// SLACK_API_BASE_URL pointing at the stub URL this prints.
    Chat {
        /// The user message text.
        text: String,
        /// Slack channel id to send as; must match the agent's slack_channel
        /// for the worker to route it (e.g. the value passed to deploy
        /// --slack-channel). Omit to mint a throwaway synthetic channel.
        #[arg(long)]
        channel: Option<String>,
        /// Existing thread ts to continue a conversation; omit to start a new
        /// thread. Pair with --channel to keep multi-turn context.
        #[arg(long)]
        thread: Option<String>,
        /// Valkey connection URL.
        #[arg(long, env = "VALKEY_URL", default_value = chat::DEFAULT_VALKEY_URL)]
        valkey_url: String,
        /// Stream the dispatcher enqueues onto.
        #[arg(long, env = "AGENTOS_STREAM", default_value = chat::DEFAULT_STREAM)]
        stream: String,
        /// Synthetic Slack user id for the enqueued event.
        #[arg(long, default_value = chat::DEFAULT_USER)]
        user: String,
        /// How long to wait for the worker's reply before printing diagnostics.
        #[arg(long, default_value_t = chat::DEFAULT_TIMEOUT_SECS)]
        timeout_secs: u64,
        /// Host the Slack stub binds. Use a routable host when the worker runs
        /// off-box (e.g. in-cluster).
        #[arg(long, default_value = chat::DEFAULT_LISTEN_HOST)]
        listen_host: String,
        /// Port the Slack stub binds; 0 picks an ephemeral port.
        #[arg(long, default_value_t = chat::DEFAULT_LISTEN_PORT)]
        listen_port: u16,
    },
    /// Inject a follow-up into the runner's live turn (POST /v1/steer).
    Steer {
        /// The follow-up message text.
        text: String,
        /// Synthetic Slack user id.
        #[arg(long, default_value = "U-local")]
        user: String,
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
    },
    /// Hard-stop the runner's live turn (POST /v1/interrupt).
    Interrupt {
        /// Reason recorded with the interrupt.
        #[arg(long, default_value = "user interrupt")]
        reason: String,
        /// Runner base URL (defaults to the started runner, then localhost).
        #[arg(long)]
        url: Option<String>,
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
    /// Manage the local dev stack (compose.dev.yaml: Postgres + Valkey +
    /// Langfuse + ClickHouse + MinIO + OTel).
    Local {
        #[command(subcommand)]
        action: LocalAction,
    },
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
        /// Chart path (run from the repo root for the default).
        #[arg(long, default_value = "charts/agentos")]
        chart: String,
        /// Keep the UI and Langfuse services ClusterIP instead of NodePort.
        #[arg(long)]
        no_expose: bool,
        /// Force the sealed fake-model install even when AGENTOS_MODEL_CREDENTIALS
        /// is set (dev/CI escape hatch); suppresses the fake-model warning.
        #[arg(long)]
        fake_model: bool,
        /// Extra `--set KEY=VAL` passed through to helm verbatim (repeatable).
        #[arg(long = "set", value_name = "KEY=VAL")]
        set: Vec<String>,
        /// Print the helm command that would run and exit without executing.
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
}

/// Subcommands of `agentos local`.
#[derive(Subcommand)]
enum LocalAction {
    /// Bring the dev stack up (docker compose up -d --wait) and print URLs.
    Up {
        /// Compose file (run from the repo root for the default).
        #[arg(long, default_value = local::DEFAULT_COMPOSE_FILE)]
        file: String,
        /// Print the docker compose command and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
    /// Stop the dev stack (docker compose down), keeping volumes.
    Down {
        /// Compose file (run from the repo root for the default).
        #[arg(long, default_value = local::DEFAULT_COMPOSE_FILE)]
        file: String,
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
        /// Compose file (run from the repo root for the default).
        #[arg(long, default_value = local::DEFAULT_COMPOSE_FILE)]
        file: String,
        /// Print the docker compose command and exit without executing.
        #[arg(long)]
        dry_run: bool,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ui::init(Ui::from_process(cli.color, cli.debug, cli.quiet));
    match cli.command {
        Command::Init { name, dir } => commands::init(&name, dir),
        Command::Start {
            plugin_dir,
            image,
            port,
            name,
            fake_model,
            network,
            otel_endpoint,
            budget,
            model,
        } => {
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
            })
            .await
        }
        Command::Stop => commands::stop().await,
        Command::RunnerStatus { url } => commands::status(url).await,
        Command::Send {
            text,
            user,
            event_type,
            url,
        } => commands::send(&text, &user, event_type.into(), url).await,
        Command::Eval { cases, url } => commands::eval(cases, url).await,
        Command::Message {
            text,
            channel,
            thread,
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
            local,
            api_url,
        } => {
            message::message(MessageOpts {
                text,
                channel,
                thread,
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
                wire: !no_wire,
                force_wire,
                dry_run,
                local,
                api_url,
            })
            .await
        }
        Command::Chat {
            text,
            channel,
            thread,
            valkey_url,
            stream,
            user,
            timeout_secs,
            listen_host,
            listen_port,
        } => {
            chat::chat(ChatOpts {
                text,
                channel,
                thread,
                valkey_url,
                stream,
                user,
                timeout_secs,
                listen_host,
                listen_port,
            })
            .await
        }
        Command::Steer { text, user, url } => commands::steer(&text, &user, url).await,
        Command::Interrupt { reason, url } => commands::interrupt(&reason, url).await,
        Command::Deploy {
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
        Command::Local { action } => match action {
            LocalAction::Up { file, dry_run } => local::up(LocalOpts { file, dry_run }).await,
            LocalAction::Down {
                file,
                wipe,
                yes,
                dry_run,
            } => {
                local::down(LocalDownOpts {
                    common: LocalOpts { file, dry_run },
                    wipe,
                    yes,
                })
                .await
            }
            LocalAction::Status { file, dry_run } => {
                local::status(LocalOpts { file, dry_run }).await
            }
        },
        Command::Up {
            namespace,
            release,
            chart,
            no_expose,
            fake_model,
            set,
            dry_run,
        } => {
            let credentials = ops::resolve_up_credentials(
                fake_model,
                std::env::var("AGENTOS_MODEL_CREDENTIALS").ok(),
            );
            ops::up(UpOpts {
                common: CommonOpts {
                    namespace,
                    release,
                    dry_run,
                },
                chart,
                no_expose,
                set,
                fake_model,
                credentials,
            })
            .await
        }
        Command::Status {
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
        Command::Down {
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
    fn message_local_composes_with_channel_thread_and_timeout() {
        let cli = Cli::try_parse_from([
            "agentos",
            "message",
            "--local",
            "--channel",
            "C123",
            "--thread",
            "1.2",
            "--timeout-secs",
            "42",
            "hello",
        ])
        .expect("--local composes with the shared flags");
        match cli.command {
            Command::Message {
                local,
                channel,
                thread,
                timeout_secs,
                ..
            } => {
                assert!(local);
                assert_eq!(channel.as_deref(), Some("C123"));
                assert_eq!(thread.as_deref(), Some("1.2"));
                assert_eq!(timeout_secs, 42);
            }
            _ => panic!("expected the message subcommand"),
        }
    }

    #[test]
    fn message_local_accepts_api_url() {
        let cli = Cli::try_parse_from([
            "agentos",
            "message",
            "--local",
            "--api-url",
            "http://localhost:9999",
            "hi",
        ])
        .expect("--api-url is allowed with --local");
        match cli.command {
            Command::Message { api_url, .. } => {
                assert_eq!(api_url.as_deref(), Some("http://localhost:9999"))
            }
            _ => panic!("expected the message subcommand"),
        }
    }

    #[test]
    fn message_local_rejects_every_cluster_only_flag() {
        // Each cluster-only flag must conflict with --local so a mixed invocation
        // fails loudly instead of silently ignoring half the intent.
        let cases: &[&[&str]] = &[
            &["--namespace", "agentos"],
            &["--release", "agentos"],
            &["--chart", "charts/agentos"],
            &["--listen-host", "1.2.3.4"],
            &["--listen-port", "9000"],
            &["--valkey-local-port", "5555"],
            &["--api-local-port", "5556"],
            &["--no-wire"],
            &["--force-wire"],
        ];
        for extra in cases {
            let mut argv = vec!["agentos", "message", "--local"];
            argv.extend_from_slice(extra);
            argv.push("hi");
            // `Cli` is not Debug, so match rather than expect_err on the Ok arm.
            let err = match Cli::try_parse_from(&argv) {
                Ok(_) => panic!("--local must reject {extra:?}"),
                Err(err) => err,
            };
            assert_eq!(
                err.kind(),
                clap::error::ErrorKind::ArgumentConflict,
                "{extra:?} should conflict with --local, got {err}"
            );
        }
    }

    #[test]
    fn api_url_requires_local() {
        let argv = [
            "agentos",
            "message",
            "--api-url",
            "http://localhost:28000",
            "hi",
        ];
        let err = match Cli::try_parse_from(argv) {
            Ok(_) => panic!("--api-url without --local is rejected"),
            Err(err) => err,
        };
        assert_eq!(err.kind(), clap::error::ErrorKind::MissingRequiredArgument);
    }
}
