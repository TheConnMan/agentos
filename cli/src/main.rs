//! The `agentos` binary: init/start/send/eval a plugin against a local runner,
//! plus stop/status for the container lifecycle and deploy for the platform
//! API. Task I1; contracts are frozen in packages/aci-protocol and
//! packages/plugin-format.

use std::path::PathBuf;

use agentos::commands::{self, DeployEnv, DeployOpts, SendType, StartOpts, DEFAULT_PORT};
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
    },
    /// Stop and remove the local runner container.
    Stop,
    /// Show the local runner's session status.
    Status,
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
        /// Eval case file.
        #[arg(long, default_value = "evals/cases.json")]
        cases: PathBuf,
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
        /// Slack channel used when the agent is first created.
        #[arg(long, default_value = "#local-dev")]
        slack_channel: String,
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
            })
            .await
        }
        Command::Stop => commands::stop().await,
        Command::Status => commands::status().await,
        Command::Send {
            text,
            user,
            event_type,
            url,
        } => commands::send(&text, &user, event_type.into(), url).await,
        Command::Eval { cases, url } => commands::eval(&cases, url).await,
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
