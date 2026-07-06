//! The `agentos` binary: init/start/send/eval a plugin against a local runner,
//! stop/status/steer/interrupt for the container lifecycle, chat to drive the
//! whole system with the CLI acting as the Slack service, and deploy for the
//! platform API. Task I1; contracts are frozen in packages/aci-protocol and
//! packages/plugin-format.

use std::path::PathBuf;

use agentos::chat::{self, ChatOpts};
use agentos::commands::{self, DeployEnv, DeployOpts, SendType, StartOpts, DEFAULT_PORT};
use agentos::slack_sim::{self, SlackSimOpts};
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
    Status {
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
    /// Real-Slack egress without Socket Mode: post a synthetic thread as the bot
    /// in a real channel, enqueue the dispatcher's event onto Valkey, and poll
    /// conversations.replies until the worker edits the placeholder. For the
    /// no-Slack variant use `chat`.
    SlackSim {
        /// The simulated user message text.
        text: String,
        /// Slack channel id to post into.
        #[arg(long, env = "AGENTOS_SLACK_CHANNEL")]
        channel: String,
        /// Slack bot token (xoxb-...). Never printed.
        #[arg(long, env = "SLACK_BOT_TOKEN", hide_env_values = true)]
        bot_token: String,
        /// Valkey connection URL.
        #[arg(long, env = "VALKEY_URL", default_value = slack_sim::DEFAULT_VALKEY_URL)]
        valkey_url: String,
        /// Stream the dispatcher enqueues onto.
        #[arg(long, env = "AGENTOS_STREAM", default_value = slack_sim::DEFAULT_STREAM)]
        stream: String,
        /// Synthetic Slack user id for the enqueued event.
        #[arg(long, default_value = slack_sim::DEFAULT_USER)]
        user: String,
        /// How long to wait for the worker's reply before printing diagnostics.
        #[arg(long, default_value_t = slack_sim::DEFAULT_TIMEOUT_SECS)]
        timeout_secs: u64,
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
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
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
        Command::Status { url } => commands::status(url).await,
        Command::Send {
            text,
            user,
            event_type,
            url,
        } => commands::send(&text, &user, event_type.into(), url).await,
        Command::Eval { cases, url } => commands::eval(cases, url).await,
        Command::SlackSim {
            text,
            channel,
            bot_token,
            valkey_url,
            stream,
            user,
            timeout_secs,
        } => {
            slack_sim::slack_sim(SlackSimOpts {
                text,
                channel,
                bot_token,
                valkey_url,
                stream,
                user,
                timeout_secs,
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
    }
}
