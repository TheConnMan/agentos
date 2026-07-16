//! Shared target noun message forms: drive the DEPLOYED Kubernetes release end
//! to end from the CLI with zero Slack contact.
//!
//! Product rationale: a developer building an agent for someone else's Slack
//! workspace can exercise the entire deployed machinery (Valkey queue -> worker
//! -> claimed sandbox -> the real skill -> the reply) without any Slack access,
//! tokens, or workspace. It is the retained chat helper engine, backed by a
//! local Slack Web API stub plus the frozen `QueuedTurn` enqueue and the
//! ack-based completion signal, with Kubernetes-aware auto-plumbing bolted on
//! top:
//!
//! 1. Self-managed `kubectl port-forward`s (children of this process, killed on
//!    exit) reach the in-cluster Valkey (for the enqueue) and API (for the
//!    default-channel lookup) with no manual setup.
//! 2. The stub binds `0.0.0.0` and advertises a routable host (either
//!    `--listen-host` or the local IP the kernel would use to reach the cluster)
//!    so the in-cluster worker can post its placeholder edits back to it.
//! 3. Each enqueued turn carries its reply endpoint (this stub's URL) on the
//!    queue payload's reply handle (issue #19), so the worker finalizes THIS
//!    turn against the stub without re-pointing its worker-global Slack setting.
//!    A real Slack workspace (whose turns carry no endpoint and so use the
//!    worker default) and this driver can therefore run against one worker at
//!    once, instead of contending for a single `worker.slackApiBaseUrl`.

use std::env;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use redis::aio::MultiplexedConnection;

use crate::api::{Agent, ApiClient};
use crate::chat::{
    await_reply, continue_hint_line, continue_hint_long_line, resolve_targets, Outcome, SlackStub,
};
use crate::evals::{EvalCase, EvalSuite};
use crate::ops::{plain, require_on_path, run_capture, OpsCommand};
use crate::queue::{self, connect, diagnostics, synthetic_turn, xadd};
use crate::state::{save_turn, TurnContext, TurnVerb};

pub const DEFAULT_STREAM: &str = queue::DEFAULT_STREAM;
pub const DEFAULT_USER: &str = "U-agentos-message";
pub const DEFAULT_TIMEOUT_SECS: u64 = 300;
/// Fixed stub port so the advertised URL is deterministic; `0` picks ephemeral.
pub const DEFAULT_LISTEN_PORT: u16 = 8155;
/// Local port the Valkey port-forward binds. Chosen to dodge the compose dev
/// stack, which squats 26379.
pub const DEFAULT_VALKEY_LOCAL_PORT: u16 = 56381;
/// Local port the API port-forward binds (only used for the default-channel
/// lookup when `--channel` is omitted).
pub const DEFAULT_API_LOCAL_PORT: u16 = 8123;
/// The chart's default Valkey password (values.yaml `valkey.password`).
pub const DEFAULT_VALKEY_PASSWORD: &str = "valkeypass";
/// The chart's default platform API key (values.yaml `api.apiKey`).
pub const DEFAULT_API_KEY: &str = "agentos-dev-key";

/// Local mode (`--local`): the compose Valkey's published host port
/// (`compose.dev.yaml`), where the CLI enqueues and the compose worker consumes.
pub const DEFAULT_LOCAL_VALKEY_PORT: u16 = 26379;
/// Local mode: the compose API's published host port (`compose.dev.yaml`
/// `agentos-api`), reached directly (routers mount at root, so no `/api`).
pub const DEFAULT_LOCAL_API_URL: &str = "http://localhost:28000";
/// Local mode: the fixed port the stub binds. It MUST equal the port in the
/// compose worker's `SLACK_API_BASE_URL` (`http://localhost:8155/api/`) so the
/// containerized worker's placeholder edits reach this stub. Same value as the
/// cluster-mode `DEFAULT_LISTEN_PORT`; the `local_stub_port_matches_listen_port`
/// test pins the coupling.
pub const DEFAULT_LOCAL_STUB_PORT: u16 = DEFAULT_LISTEN_PORT;

/// In-cluster service ports the port-forwards target.
const VALKEY_REMOTE_PORT: u16 = 6379;
const API_REMOTE_PORT: u16 = 8000;

/// Options for the shared target noun message forms, mirroring their clap
/// flags.
pub struct MessageOpts {
    pub text: String,
    pub channel: Option<String>,
    pub thread: Option<String>,
    pub namespace: String,
    pub release: String,
    pub chart: String,
    /// Host the stub advertises to the worker; `None` auto-detects the local IP.
    pub listen_host: Option<String>,
    pub listen_port: u16,
    pub valkey_local_port: u16,
    pub valkey_password: String,
    pub api_local_port: u16,
    pub api_key: String,
    pub user: String,
    pub stream: String,
    pub timeout_secs: u64,
    pub dry_run: bool,
    /// Local mode: drive the compose stack (`agentos local up`) instead of a
    /// Kubernetes release. No kubectl/helm/port-forwards/wiring; enqueue straight
    /// to the compose Valkey and let the containerized worker answer.
    pub local: bool,
    /// Local mode only: platform API base URL for the channel lookup. None uses
    /// the compose API default ([`DEFAULT_LOCAL_API_URL`]).
    pub api_url: Option<String>,
}

fn persist_and_hint(opts: &MessageOpts, verb: TurnVerb, channel: &str, thread_ts: &str) {
    let ui = crate::ui::ui();
    let verb_str = match verb {
        TurnVerb::Local => "local message",
        TurnVerb::Cluster => "cluster message",
    };
    let ctx = TurnContext::from_turn(
        opts,
        verb,
        channel,
        thread_ts,
        env::var("AGENTOS_API_KEY").ok(),
    );

    match env::current_dir().context("resolving the current working directory") {
        Ok(cwd) => match save_turn(&cwd, &ctx) {
            Ok(()) => ui.note(&continue_hint_line(verb_str)),
            Err(err) => {
                ui.warn(&format!("could not save turn context: {err}"));
                ui.note(&continue_hint_long_line(verb_str, channel, thread_ts));
            }
        },
        Err(err) => {
            ui.warn(&format!("could not save turn context: {err}"));
            ui.note(&continue_hint_long_line(verb_str, channel, thread_ts));
        }
    }
}

// ---------------------------------------------------------------------------
// Pure command builders (unit-tested below)
// ---------------------------------------------------------------------------

/// `kubectl -n <ns> port-forward svc/<release>-<suffix> <local>:<remote>`.
pub fn port_forward_command(
    namespace: &str,
    release: &str,
    suffix: &str,
    local_port: u16,
    remote_port: u16,
) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("-n"),
            plain(namespace),
            plain("port-forward"),
            plain(format!("svc/{release}-{suffix}")),
            plain(format!("{local_port}:{remote_port}")),
        ],
    )
}

/// `kubectl config view --minify -o jsonpath={...server}`: the kubeconfig's
/// current-context API server URL, used to pick the local egress IP.
fn server_url_command() -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("config"),
            plain("view"),
            plain("--minify"),
            plain("-o"),
            plain("jsonpath={.clusters[0].cluster.server}"),
        ],
    )
}

/// The `/api/` base URL the worker posts its placeholder edits to.
fn advertised_url(host: &str, port: u16) -> String {
    format!("http://{host}:{port}/api/")
}

/// Local mode: the Valkey URL the CLI enqueues onto -- the compose Valkey on its
/// published host port, authenticated with the same password the compose worker
/// uses. Pure so the construction is unit-tested without a live Valkey.
pub fn local_valkey_url(password: &str) -> String {
    format!("redis://:{password}@localhost:{DEFAULT_LOCAL_VALKEY_PORT}")
}

/// Local mode: the platform API base for the channel lookup -- an explicit
/// `--api-url` wins, else the compose API default.
pub fn local_api_base(api_url: Option<&str>) -> String {
    api_url.unwrap_or(DEFAULT_LOCAL_API_URL).to_string()
}

/// Pick the channel to send as: an explicit `--channel` wins; otherwise the sole
/// deployed agent's `slack_channel`. Zero or multiple agents is an error naming
/// them and requiring `--channel`, because the worker binds a channel to an
/// agent by exact equality -- guessing would silently route nowhere.
pub fn select_channel(agents: &[Agent], explicit: Option<&str>) -> Result<String> {
    if let Some(channel) = explicit {
        return Ok(channel.to_string());
    }
    match agents {
        [] => bail!(
            "no agents are deployed on the platform API; deploy one with `agentos local deploy` \
             or `agentos cluster deploy`, or pass --channel <id>"
        ),
        [only] => Ok(only.slack_channel.clone()),
        many => {
            let listed = many
                .iter()
                .map(|a| format!("{} -> {}", a.name, a.slack_channel))
                .collect::<Vec<_>>()
                .join(", ");
            bail!("multiple agents are deployed; pass --channel <id> to pick one ({listed})")
        }
    }
}

/// Parse a kubeconfig `cluster.server` URL into its host and optional raw port,
/// stripping the scheme and any path and correctly handling bracketed IPv6
/// authorities (`https://[::1]:6443` -> ("::1", Some("6443"))). Returns `None`
/// when no host remains. The port is returned unparsed; callers decide how to
/// default or validate it.
pub(crate) fn split_server_url(server: &str) -> Option<(&str, Option<&str>)> {
    let rest = server
        .strip_prefix("https://")
        .or_else(|| server.strip_prefix("http://"))
        .unwrap_or(server);
    let authority = rest.split('/').next().unwrap_or(rest).trim();
    if authority.is_empty() {
        return None;
    }
    let (host, port) = if let Some(after_bracket) = authority.strip_prefix('[') {
        // Bracketed IPv6: the host is between '[' and ']'; an optional ':port'
        // may follow the closing bracket.
        let (host, tail) = after_bracket.split_once(']')?;
        let port = tail.strip_prefix(':').filter(|p| !p.is_empty());
        (host, port)
    } else {
        match authority.rsplit_once(':') {
            Some((h, p)) if !h.is_empty() => (h, Some(p)),
            _ => (authority, None),
        }
    };
    let host = host.trim();
    (!host.is_empty()).then_some((host, port))
}

/// Split a kubeconfig server URL into (host, port), defaulting the port from the
/// scheme when absent (`https://10.1.2.3:6443` -> `("10.1.2.3", 6443)`). Used
/// only to pick a UDP-connect target for local-IP detection, so the exact port
/// barely matters (any routable port to the same host selects the same source
/// interface).
pub fn server_host_and_port(server: &str) -> Option<(String, u16)> {
    let default_port = if server.starts_with("http://") {
        80
    } else {
        443
    };
    let (host, port) = split_server_url(server)?;
    let port = match port {
        Some(p) => p.parse().ok()?,
        None => default_port,
    };
    Some((host.to_string(), port))
}

/// The ordered command lines (plus the stub URL and enqueue description) that a
/// real run would execute, for `--dry-run`. Pure so the rendering is testable.
/// The reply routes back to the stub via the per-turn endpoint on the queue
/// payload (issue #19), so there is no worker-global `helm upgrade` to render.
pub fn dry_run_lines(opts: &MessageOpts, advertise_host: &str) -> Vec<String> {
    let mut cmds: Vec<OpsCommand> = vec![port_forward_command(
        &opts.namespace,
        &opts.release,
        "valkey",
        opts.valkey_local_port,
        VALKEY_REMOTE_PORT,
    )];
    if opts.channel.is_none() {
        cmds.push(port_forward_command(
            &opts.namespace,
            &opts.release,
            "api",
            opts.api_local_port,
            API_REMOTE_PORT,
        ));
    }
    let url = advertised_url(advertise_host, opts.listen_port);
    let mut lines: Vec<String> = cmds.iter().map(OpsCommand::display).collect();
    lines.push(format!("stub advertised at {url}"));
    let channel = opts
        .channel
        .clone()
        .unwrap_or_else(|| "<the sole deployed agent's slack_channel>".to_string());
    lines.push(format!(
        "enqueue a synthetic QueuedTurn (reply endpoint {url}) for channel {channel} \
         on stream {}",
        opts.stream
    ));
    lines
}

/// The machine-readable reply object for `local`/`cluster message --json`
/// (issue #353): the model's reply text (null when the worker finished without
/// editing the placeholder), the thread the turn ran under, and whether a reply
/// was captured. Pure so it stays contract-testable against
/// `cli/schema/message.schema.json`.
pub fn message_reply_json(thread: &str, reply: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "reply": reply,
        "thread": thread,
        "finalized": reply.is_some(),
    })
}

/// The machine-readable object for a `local`/`cluster message --json` **timeout**
/// (issue #354): no reply was captured before the deadline, so `reply` is null,
/// `finalized` is false, and `timed_out` marks the terminal state distinctly from
/// a no-edit completion. Emitted just before the transient exit so a `--json`
/// caller gets a structured line, not empty stdout. Pure so it stays
/// contract-testable against `cli/schema/message.schema.json`.
pub fn message_timeout_json() -> serde_json::Value {
    serde_json::json!({
        "reply": serde_json::Value::Null,
        "finalized": false,
        "timed_out": true,
    })
}

/// The machine-readable descriptor for `local`/`cluster message --json --dry-run`
/// (issue #354): what a real run would enqueue, without touching the network.
/// `target` is `"local"` or `"cluster"`, `channel` is null when it would be
/// resolved from the sole deployed agent. Pure so it stays contract-testable
/// against `cli/schema/message.schema.json`.
pub fn message_dry_run_json(
    target: &str,
    stream: &str,
    channel: Option<&str>,
    reply_endpoint: &str,
) -> serde_json::Value {
    serde_json::json!({
        "dry_run": true,
        "target": target,
        "stream": stream,
        "channel": channel,
        "reply_endpoint": reply_endpoint,
    })
}

// ---------------------------------------------------------------------------
// Effectful helpers
// ---------------------------------------------------------------------------

/// The routable host the stub advertises: `--listen-host` verbatim, otherwise
/// the local IP the kernel would use to reach the cluster's API server (via a
/// UDP-connect that sends no packets -- it only resolves the source interface).
async fn resolve_advertise_host(listen_host: Option<&str>) -> Result<String> {
    if let Some(host) = listen_host {
        return Ok(host.to_string());
    }
    let (ok, out, err) = run_capture(&server_url_command()).await?;
    if !ok {
        bail!(
            "could not read the kubeconfig API server to auto-detect a routable host ({}); \
             pass --listen-host <host>",
            err.trim()
                .lines()
                .next()
                .unwrap_or("kubectl config view failed")
        );
    }
    let server = out.trim();
    let (host, port) = server_host_and_port(server).with_context(|| {
        format!("could not parse the kubeconfig server url {server:?}; pass --listen-host <host>")
    })?;
    let ip = detect_local_ip(&host, port).with_context(|| {
        format!("could not detect the local IP toward {host}:{port}; pass --listen-host <host>")
    })?;
    Ok(ip.to_string())
}

/// The local source IP the kernel would use to reach `host:port`. A UDP socket
/// `connect` only sets the default peer and picks the egress interface; no
/// datagram is sent, so this needs no reachability and touches no network.
fn detect_local_ip(host: &str, port: u16) -> Option<std::net::IpAddr> {
    let socket = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect((host, port)).ok()?;
    socket.local_addr().ok().map(|addr| addr.ip())
}

/// Spawn a `kubectl port-forward` child (killed on drop via `kill_on_drop`) and
/// block until its local port accepts TCP, so callers can use it immediately.
async fn start_port_forward(
    cmd: &OpsCommand,
    local_port: u16,
    label: &str,
) -> Result<tokio::process::Child> {
    crate::ui::ui().plumbing(&format!("+ {}", cmd.display()));
    let child = tokio::process::Command::new(&cmd.program)
        .args(cmd.argv())
        .kill_on_drop(true)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .with_context(|| format!("spawning `{}` (is kubectl on PATH?)", cmd.program))?;
    wait_for_tcp(local_port, Duration::from_secs(15))
        .await
        .with_context(|| format!("the {label} port-forward never opened localhost:{local_port}"))?;
    Ok(child)
}

/// Poll-connect to `localhost:port` until it accepts or the timeout elapses.
async fn wait_for_tcp(port: u16, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            bail!(
                "timed out after {:?} connecting to localhost:{port}",
                timeout
            );
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

/// The `agentos local message` handler: drive the compose stack directly.
///
/// The cluster path's self-plumbing (kubectl port-forwards) is cluster-specific,
/// so local mode keeps only the shared engine: bind the Slack stub, enqueue the
/// `QueuedTurn` carrying this stub as its reply endpoint (issue #19), and wait on
/// the XACK signal. The compose worker reaches the stub on the fixed loopback
/// port `http://localhost:{DEFAULT_LOCAL_STUB_PORT}/api/`.
async fn message_local(opts: MessageOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let valkey_url = local_valkey_url(&opts.valkey_password);
    let api_base = local_api_base(opts.api_url.as_deref());

    if opts.dry_run {
        let reply_endpoint = format!("http://localhost:{DEFAULT_LOCAL_STUB_PORT}/api/");
        if ui.json() {
            ui.emit_json(&message_dry_run_json(
                "local",
                &opts.stream,
                opts.channel.as_deref(),
                &reply_endpoint,
            ));
            return Ok(());
        }
        ui.payload_plain("local mode (compose stack; no kubectl/helm)");
        ui.payload_plain(&format!("enqueue onto redis {valkey_url}"));
        ui.payload_plain(&format!("stub advertised at {reply_endpoint}"));
        match opts.channel.as_deref() {
            Some(channel) => ui.payload_plain(&format!("channel {channel}")),
            None => ui.payload_plain(&format!(
                "channel <the sole deployed agent via {api_base}/agents>"
            )),
        }
        ui.payload_plain(&format!(
            "enqueue a synthetic QueuedTurn on stream {}",
            opts.stream
        ));
        return Ok(());
    }

    // Connect Valkey up front so a down stack fails fast, before the stub binds.
    let mut conn = connect(&valkey_url).await?;

    // Bind the stub on loopback at the fixed port the compose worker posts to.
    // Host networking puts the worker on the same loopback, so 127.0.0.1 both
    // binds and is reachable; the advertised host is cosmetic here (the worker's
    // base URL is fixed in compose, not taken from this print).
    let mut stub = SlackStub::start("127.0.0.1", DEFAULT_LOCAL_STUB_PORT, "localhost").await?;
    ui.note(&format!(
        "slack stub listening; the worker posts to {}",
        stub.base_api_url()
    ));

    // Channel: explicit --channel, else the sole deployed agent from the compose
    // API (reached directly; routers mount at root, so the base carries no /api).
    let channel = match opts.channel.as_deref() {
        Some(channel) => channel.to_string(),
        None => {
            let api = ApiClient::new(&api_base, &opts.api_key)?;
            let agents = api.list_agents().await.with_context(|| {
                format!("listing agents via {api_base} (is `agentos local up` running?)")
            })?;
            select_channel(&agents, None)?
        }
    };
    ui.note(&format!("routing to channel {channel}"));

    // This turn carries its own reply endpoint (issue #19), so the compose worker
    // finalizes it against this stub without relying on a worker-global setting.
    let reply_endpoint = stub.base_api_url().to_string();
    let (channel, thread_ts, placeholder_ts) =
        resolve_targets(Some(&channel), opts.thread.as_deref());
    let event = synthetic_turn(
        &channel,
        &opts.user,
        &opts.text,
        &thread_ts,
        &placeholder_ts,
        Some(reply_endpoint),
    );
    let stream_id = xadd(&mut conn, &opts.stream, &event).await?;
    ui.note(&format!(
        "enqueued {} on {} as {stream_id}",
        event.event_id, opts.stream
    ));
    ui.note(&format!(
        "waiting up to {}s for the worker to finalize the turn...",
        opts.timeout_secs
    ));

    let cl = ui.checklist();
    let step = cl.step("waiting for worker reply");
    let outcome = await_reply(
        &mut stub,
        &mut conn,
        &opts.stream,
        &stream_id,
        &placeholder_ts,
        Duration::from_secs(opts.timeout_secs),
    )
    .await;

    match outcome {
        Outcome::Replied(reply) => {
            step.done("");
            if ui.json() {
                ui.emit_json(&message_reply_json(&thread_ts, Some(&reply)));
            } else {
                ui.answer(&reply);
                ui.print_tokens("\n");
            }
            persist_and_hint(&opts, TurnVerb::Local, &channel, &thread_ts);
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            step.done("no edit");
            if ui.json() {
                ui.emit_json(&message_reply_json(&thread_ts, None));
            } else {
                ui.warn("the worker finished the turn but never edited the placeholder");
            }
            persist_and_hint(&opts, TurnVerb::Local, &channel, &thread_ts);
            Ok(())
        }
        Outcome::TimedOut => {
            step.fail(&format!("timed out after {}s", opts.timeout_secs));
            if ui.json() {
                ui.emit_json(&message_timeout_json());
            } else {
                ui.note("stream diagnostics:");
                let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
                ui.note(&diag);
            }
            // A timeout is retryable (the worker may still be working, or a
            // transient stall), so it maps to the transient exit code, not failure.
            std::process::exit(crate::exit::ExitClass::Transient.code());
        }
    }
}

/// The shared target noun message handler.
pub async fn message(opts: MessageOpts) -> Result<()> {
    if opts.local {
        return message_local(opts).await;
    }
    let ui = crate::ui::ui();
    if opts.dry_run {
        let host = opts
            .listen_host
            .clone()
            .unwrap_or_else(|| "<auto-detected-local-ip>".to_string());
        if ui.json() {
            ui.emit_json(&message_dry_run_json(
                "cluster",
                &opts.stream,
                opts.channel.as_deref(),
                &advertised_url(&host, opts.listen_port),
            ));
            return Ok(());
        }
        for line in dry_run_lines(&opts, &host) {
            ui.payload_plain(&line);
        }
        return Ok(());
    }

    require_on_path("kubectl")?;

    // Advertise a host the in-cluster worker can reach, then bind the stub on
    // 0.0.0.0 so it is reachable off-box. Take the URL from the started stub so an
    // ephemeral --listen-port 0 still yields the real bound port.
    let advertise_host = resolve_advertise_host(opts.listen_host.as_deref()).await?;
    let mut stub = SlackStub::start("0.0.0.0", opts.listen_port, &advertise_host).await?;
    let url = stub.base_api_url().to_string();
    ui.note(&format!(
        "slack stub listening; the worker will post to {url}"
    ));

    // Valkey port-forward for the enqueue (killed on drop at fn end).
    let _valkey_pf = start_port_forward(
        &port_forward_command(
            &opts.namespace,
            &opts.release,
            "valkey",
            opts.valkey_local_port,
            VALKEY_REMOTE_PORT,
        ),
        opts.valkey_local_port,
        "valkey",
    )
    .await?;

    // Channel: explicit --channel, else the sole deployed agent via a short-lived
    // API port-forward (dropped once the lookup returns).
    let channel = match opts.channel.as_deref() {
        Some(channel) => channel.to_string(),
        None => {
            let _api_pf = start_port_forward(
                &port_forward_command(
                    &opts.namespace,
                    &opts.release,
                    "api",
                    opts.api_local_port,
                    API_REMOTE_PORT,
                ),
                opts.api_local_port,
                "api",
            )
            .await?;
            let api = ApiClient::new(
                &format!("http://localhost:{}", opts.api_local_port),
                &opts.api_key,
            )?;
            let agents = api
                .list_agents()
                .await
                .context("listing agents through the api port-forward")?;
            select_channel(&agents, None)?
        }
    };
    ui.note(&format!("routing to channel {channel}"));

    // Enqueue the exact event the dispatcher would produce and wait for the ack.
    // The turn carries its reply endpoint (this stub's advertised URL) on the
    // payload (issue #19), so the in-cluster worker posts THIS turn's reply back
    // to the stub without a worker-global `helm upgrade`; a real workspace on the
    // same worker keeps replying to real Slack.
    let valkey_url = format!(
        "redis://:{}@localhost:{}",
        opts.valkey_password, opts.valkey_local_port
    );
    let mut conn = connect(&valkey_url).await?;
    let (channel, thread_ts, placeholder_ts) =
        resolve_targets(Some(&channel), opts.thread.as_deref());
    let event = synthetic_turn(
        &channel,
        &opts.user,
        &opts.text,
        &thread_ts,
        &placeholder_ts,
        Some(url),
    );
    let stream_id = xadd(&mut conn, &opts.stream, &event).await?;
    ui.note(&format!(
        "enqueued {} on {} as {stream_id}",
        event.event_id, opts.stream
    ));
    ui.note(&format!(
        "waiting up to {}s for the worker to finalize the turn...",
        opts.timeout_secs
    ));

    let cl = ui.checklist();
    let step = cl.step("waiting for worker reply");
    let outcome = await_reply(
        &mut stub,
        &mut conn,
        &opts.stream,
        &stream_id,
        &placeholder_ts,
        Duration::from_secs(opts.timeout_secs),
    )
    .await;

    match outcome {
        Outcome::Replied(reply) => {
            step.done("");
            if ui.json() {
                ui.emit_json(&message_reply_json(&thread_ts, Some(&reply)));
            } else {
                ui.answer(&reply);
                ui.print_tokens("\n");
            }
            persist_and_hint(&opts, TurnVerb::Cluster, &channel, &thread_ts);
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            step.done("no edit");
            if ui.json() {
                ui.emit_json(&message_reply_json(&thread_ts, None));
            } else {
                ui.warn("the worker finished the turn but never edited the placeholder");
            }
            persist_and_hint(&opts, TurnVerb::Cluster, &channel, &thread_ts);
            Ok(())
        }
        Outcome::TimedOut => {
            step.fail(&format!("timed out after {}s", opts.timeout_secs));
            if ui.json() {
                ui.emit_json(&message_timeout_json());
            } else {
                ui.note("stream diagnostics:");
                let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
                ui.note(&diag);
            }
            // A timeout is retryable (the worker may still be working, or a
            // transient stall), so it maps to the transient exit code, not failure.
            std::process::exit(crate::exit::ExitClass::Transient.code());
        }
    }
}

// ---------------------------------------------------------------------------
// eval: the same evals/cases.json at the local and cluster tiers
// ---------------------------------------------------------------------------

/// Options for `agentos local eval` / `agentos cluster eval`, mirroring their
/// clap flags. The connection surface is the `message` subset (no per-turn
/// `text`/`thread`); `cases` selects the suite and `local` picks the tier.
pub struct EvalOpts {
    /// Explicit eval-case file; `None` resolves `evals/cases.json` like
    /// `skill eval` (cwd, then the recorded bundle dir).
    pub cases: Option<PathBuf>,
    pub channel: Option<String>,
    pub namespace: String,
    pub release: String,
    pub listen_host: Option<String>,
    pub listen_port: u16,
    pub valkey_local_port: u16,
    pub valkey_password: String,
    pub api_local_port: u16,
    pub api_key: String,
    pub user: String,
    pub stream: String,
    pub timeout_secs: u64,
    pub dry_run: bool,
    /// Local mode: drive the compose stack instead of a Kubernetes release.
    pub local: bool,
    /// Local mode only: platform API base URL for the channel lookup.
    pub api_url: Option<String>,
}

/// Grade one tier turn's reply with the SAME grader `skill eval` uses. A turn
/// passes only when the worker finalized it WITH reply text (`Replied`) and that
/// text satisfies the case's grader -- the exact condition `evals::turn_passes`
/// enforces over the runner's event stream, applied here to the reply the
/// message enqueue+await path captures. `CompletedNoEdit` (finished, no
/// placeholder edit) and `TimedOut` never pass, mirroring `turn_passes`'s
/// requirement that the turn reach a `done` final.
pub fn reply_passes(case: &EvalCase, outcome: &Outcome) -> bool {
    match outcome {
        Outcome::Replied(reply) => case.grader.grade(reply),
        Outcome::CompletedNoEdit | Outcome::TimedOut => false,
    }
}

/// The plan a `--dry-run` eval prints: the tier, the suite/case count, and the
/// same enqueue/port-forward description a real run would produce. Pure so the
/// rendering is unit-testable with no stack or cluster (mirrors `dry_run_lines`).
pub fn eval_dry_run_lines(opts: &EvalOpts, suite_name: &str, case_count: usize) -> Vec<String> {
    let tier = if opts.local { "local" } else { "cluster" };
    let mut lines = vec![format!(
        "grade {case_count} case(s) from suite {suite_name:?} against the {tier} tier"
    )];
    if opts.local {
        let valkey_url = local_valkey_url(&opts.valkey_password);
        let api_base = local_api_base(opts.api_url.as_deref());
        lines.push("local mode (compose stack; no kubectl/helm)".to_string());
        lines.push(format!("enqueue onto redis {valkey_url}"));
        lines.push(format!(
            "stub advertised at http://localhost:{DEFAULT_LOCAL_STUB_PORT}/api/"
        ));
        match opts.channel.as_deref() {
            Some(channel) => lines.push(format!("channel {channel}")),
            None => lines.push(format!(
                "channel <the sole deployed agent via {api_base}/agents>"
            )),
        }
    } else {
        let host = opts
            .listen_host
            .clone()
            .unwrap_or_else(|| "<auto-detected-local-ip>".to_string());
        lines.push(
            port_forward_command(
                &opts.namespace,
                &opts.release,
                "valkey",
                opts.valkey_local_port,
                VALKEY_REMOTE_PORT,
            )
            .display(),
        );
        if opts.channel.is_none() {
            lines.push(
                port_forward_command(
                    &opts.namespace,
                    &opts.release,
                    "api",
                    opts.api_local_port,
                    API_REMOTE_PORT,
                )
                .display(),
            );
        }
        lines.push(format!(
            "stub advertised at {}",
            advertised_url(&host, opts.listen_port)
        ));
    }
    lines.push(format!(
        "enqueue one synthetic QueuedTurn per case on stream {}",
        opts.stream
    ));
    lines
}

/// Resolve the eval suite the way `skill eval` does: an explicit `--cases`
/// wins, else `evals/cases.json` in the cwd, then the recorded bundle dir.
fn resolve_suite(explicit: Option<PathBuf>) -> Result<EvalSuite> {
    let state_plugin_dir = crate::state::load(Path::new("."))?.map(|s| PathBuf::from(s.plugin_dir));
    let path =
        crate::commands::resolve_cases_path(explicit, Path::new("."), state_plugin_dir.as_deref())?;
    crate::evals::load_suite(&path)
}

/// The shared per-tier eval engine: enqueue one synthetic `QueuedTurn` per case
/// through the already-stood-up stub + Valkey (the same enqueue+await path a
/// single `message` walks), grade the captured reply, and collect
/// `(id, passed, seconds)` rows for `report_eval`. Tier-agnostic: the caller
/// binds the stub/connection for its tier, then hands them here.
async fn run_eval_turns(
    opts: &EvalOpts,
    channel: &str,
    suite: &EvalSuite,
    conn: &mut MultiplexedConnection,
    stub: &mut SlackStub,
) -> Result<Vec<(String, bool, f64)>> {
    let ui = crate::ui::ui();
    let total = suite.cases.len();
    let bar = ui.progress_bar(total as u64, "running evals");
    let mut results: Vec<(String, bool, f64)> = Vec::with_capacity(total);
    for case in &suite.cases {
        // Each case is its own thread so turns never cross-talk on the stub.
        let (channel_id, thread_ts, placeholder_ts) = resolve_targets(Some(channel), None);
        let reply_endpoint = stub.base_api_url().to_string();
        let event = synthetic_turn(
            &channel_id,
            &opts.user,
            &case.input,
            &thread_ts,
            &placeholder_ts,
            Some(reply_endpoint),
        );
        let started = Instant::now();
        let stream_id = xadd(conn, &opts.stream, &event).await?;
        let outcome = await_reply(
            stub,
            conn,
            &opts.stream,
            &stream_id,
            &placeholder_ts,
            Duration::from_secs(opts.timeout_secs),
        )
        .await;
        let elapsed = started.elapsed().as_secs_f64();
        results.push((case.id.clone(), reply_passes(case, &outcome), elapsed));
        bar.inc(1);
    }
    bar.finish();
    Ok(results)
}

/// The shared `eval` handler: run the bundle's `evals/cases.json` through the
/// target tier's message enqueue+await path and grade with the shared grader,
/// so a suite that passes at `skill` can be re-asserted verbatim at `local` and
/// `cluster` (issue #344, the per-tier parity gate).
pub async fn eval(opts: EvalOpts) -> Result<()> {
    let suite = resolve_suite(opts.cases.clone())?;
    if opts.local {
        eval_local(opts, suite).await
    } else {
        eval_cluster(opts, suite).await
    }
}

async fn eval_local(opts: EvalOpts, suite: EvalSuite) -> Result<()> {
    let ui = crate::ui::ui();
    let valkey_url = local_valkey_url(&opts.valkey_password);
    let api_base = local_api_base(opts.api_url.as_deref());

    if opts.dry_run {
        for line in eval_dry_run_lines(&opts, &suite.name, suite.cases.len()) {
            ui.payload_plain(&line);
        }
        return Ok(());
    }

    let mut conn = connect(&valkey_url).await?;
    let mut stub = SlackStub::start("127.0.0.1", DEFAULT_LOCAL_STUB_PORT, "localhost").await?;
    ui.note(&format!(
        "slack stub listening; the worker posts to {}",
        stub.base_api_url()
    ));

    let channel = match opts.channel.as_deref() {
        Some(channel) => channel.to_string(),
        None => {
            let api = ApiClient::new(&api_base, &opts.api_key)?;
            let agents = api.list_agents().await.with_context(|| {
                format!("listing agents via {api_base} (is `agentos local up` running?)")
            })?;
            select_channel(&agents, None)?
        }
    };
    ui.note(&format!("routing to channel {channel}"));

    let results = run_eval_turns(&opts, &channel, &suite, &mut conn, &mut stub).await?;
    crate::commands::report_eval(&results)
}

async fn eval_cluster(opts: EvalOpts, suite: EvalSuite) -> Result<()> {
    let ui = crate::ui::ui();

    if opts.dry_run {
        for line in eval_dry_run_lines(&opts, &suite.name, suite.cases.len()) {
            ui.payload_plain(&line);
        }
        return Ok(());
    }

    require_on_path("kubectl")?;

    let advertise_host = resolve_advertise_host(opts.listen_host.as_deref()).await?;
    let mut stub = SlackStub::start("0.0.0.0", opts.listen_port, &advertise_host).await?;
    ui.note(&format!(
        "slack stub listening; the worker will post to {}",
        stub.base_api_url()
    ));

    // Valkey port-forward for the enqueue, kept alive for the whole eval loop.
    let _valkey_pf = start_port_forward(
        &port_forward_command(
            &opts.namespace,
            &opts.release,
            "valkey",
            opts.valkey_local_port,
            VALKEY_REMOTE_PORT,
        ),
        opts.valkey_local_port,
        "valkey",
    )
    .await?;

    let channel = match opts.channel.as_deref() {
        Some(channel) => channel.to_string(),
        None => {
            let _api_pf = start_port_forward(
                &port_forward_command(
                    &opts.namespace,
                    &opts.release,
                    "api",
                    opts.api_local_port,
                    API_REMOTE_PORT,
                ),
                opts.api_local_port,
                "api",
            )
            .await?;
            let api = ApiClient::new(
                &format!("http://localhost:{}", opts.api_local_port),
                &opts.api_key,
            )?;
            let agents = api
                .list_agents()
                .await
                .context("listing agents through the api port-forward")?;
            select_channel(&agents, None)?
        }
    };
    ui.note(&format!("routing to channel {channel}"));

    let valkey_url = format!(
        "redis://:{}@localhost:{}",
        opts.valkey_password, opts.valkey_local_port
    );
    let mut conn = connect(&valkey_url).await?;

    let results = run_eval_turns(&opts, &channel, &suite, &mut conn, &mut stub).await?;
    crate::commands::report_eval(&results)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(name: &str, channel: &str) -> Agent {
        Agent {
            id: format!("id-{name}"),
            name: name.to_string(),
            slack_channel: channel.to_string(),
            approval_required_tools: None,
        }
    }

    fn opts(channel: Option<&str>) -> MessageOpts {
        MessageOpts {
            text: "hi".into(),
            channel: channel.map(str::to_string),
            thread: None,
            namespace: "agentos".into(),
            release: "agentos".into(),
            chart: "charts/agentos".into(),
            listen_host: None,
            listen_port: DEFAULT_LISTEN_PORT,
            valkey_local_port: DEFAULT_VALKEY_LOCAL_PORT,
            valkey_password: DEFAULT_VALKEY_PASSWORD.into(),
            api_local_port: DEFAULT_API_LOCAL_PORT,
            api_key: DEFAULT_API_KEY.into(),
            user: DEFAULT_USER.into(),
            stream: DEFAULT_STREAM.into(),
            timeout_secs: DEFAULT_TIMEOUT_SECS,
            dry_run: false,
            local: false,
            api_url: None,
        }
    }

    #[test]
    fn port_forward_command_renders_svc_and_ports() {
        let cmd = port_forward_command("agentos", "agentos", "valkey", 56381, 6379);
        assert_eq!(
            cmd.display(),
            "kubectl -n agentos port-forward svc/agentos-valkey 56381:6379"
        );
    }

    #[test]
    fn select_channel_prefers_explicit() {
        let agents = [agent("a", "C1"), agent("b", "C2")];
        assert_eq!(select_channel(&agents, Some("CX")).unwrap(), "CX");
    }

    #[test]
    fn select_channel_uses_the_sole_agent() {
        let agents = [agent("only", "C-ONLY")];
        assert_eq!(select_channel(&agents, None).unwrap(), "C-ONLY");
    }

    #[test]
    fn select_channel_errors_on_zero_agents_naming_the_flag() {
        let err = select_channel(&[], None).unwrap_err().to_string();
        assert!(err.contains("--channel"), "{err}");
        assert!(err.contains("no agents"), "{err}");
    }

    #[test]
    fn select_channel_errors_on_many_agents_listing_them() {
        let agents = [agent("alpha", "C1"), agent("beta", "C2")];
        let err = select_channel(&agents, None).unwrap_err().to_string();
        assert!(err.contains("--channel"), "{err}");
        assert!(err.contains("alpha -> C1"), "{err}");
        assert!(err.contains("beta -> C2"), "{err}");
    }

    #[test]
    fn server_host_and_port_parses_scheme_host_port() {
        assert_eq!(
            server_host_and_port("https://10.1.2.3:6443"),
            Some(("10.1.2.3".into(), 6443))
        );
        assert_eq!(
            server_host_and_port("https://k3s.local:6443/"),
            Some(("k3s.local".into(), 6443))
        );
        // No explicit port defaults from the scheme.
        assert_eq!(
            server_host_and_port("https://host"),
            Some(("host".into(), 443))
        );
        assert_eq!(
            server_host_and_port("http://host"),
            Some(("host".into(), 80))
        );
        assert_eq!(server_host_and_port(""), None);
    }

    #[test]
    fn server_host_and_port_parses_bracketed_ipv6() {
        assert_eq!(
            server_host_and_port("https://[::1]:6443"),
            Some(("::1".to_string(), 6443))
        );
        assert_eq!(
            server_host_and_port("https://[2001:db8::1]:8443"),
            Some(("2001:db8::1".to_string(), 8443))
        );
        // No explicit port defaults from the scheme; brackets are stripped.
        assert_eq!(
            server_host_and_port("https://[::1]"),
            Some(("::1".to_string(), 443))
        );
    }

    #[test]
    fn local_valkey_url_targets_the_compose_valkey_with_the_password() {
        assert_eq!(
            local_valkey_url("valkeypass"),
            "redis://:valkeypass@localhost:26379"
        );
        // A custom password flows through unchanged.
        assert_eq!(
            local_valkey_url("s3cr3t"),
            "redis://:s3cr3t@localhost:26379"
        );
    }

    #[test]
    fn local_api_base_prefers_explicit_then_falls_back_to_compose_default() {
        assert_eq!(local_api_base(Some("http://host:9999")), "http://host:9999");
        assert_eq!(local_api_base(None), DEFAULT_LOCAL_API_URL);
        // The compose API default carries no /api (routers mount at root).
        assert!(!DEFAULT_LOCAL_API_URL.ends_with("/api"));
    }

    #[test]
    fn local_stub_port_matches_listen_port() {
        // The stub port is coupled to the compose worker's SLACK_API_BASE_URL
        // (http://localhost:8155/api/); pin it so a change to one flags the other.
        assert_eq!(DEFAULT_LOCAL_STUB_PORT, 8155);
        assert_eq!(DEFAULT_LOCAL_STUB_PORT, DEFAULT_LISTEN_PORT);
    }

    #[test]
    fn dry_run_lists_the_valkey_forward_and_the_enqueue_with_the_reply_endpoint() {
        let lines = dry_run_lines(&opts(Some("C123")), "10.1.2.3");
        // Explicit channel -> only the Valkey forward, no API forward.
        assert!(
            lines
                .iter()
                .any(|l| l == "kubectl -n agentos port-forward svc/agentos-valkey 56381:6379"),
            "{lines:?}"
        );
        assert!(
            !lines.iter().any(|l| l.contains("svc/agentos-api")),
            "explicit channel needs no api forward: {lines:?}"
        );
        // Issue #19: the reply routes per turn, so there is no worker-global
        // helm upgrade / rollout / dispatcher guard in the plan.
        assert!(
            !lines.iter().any(|l| l.contains("helm upgrade")),
            "no worker-global wiring: {lines:?}"
        );
        assert!(
            !lines.iter().any(|l| l.contains("rollout status")),
            "no rollout wait: {lines:?}"
        );
        assert!(
            !lines.iter().any(|l| l.contains("dispatcher")),
            "no dispatcher guard: {lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l == "stub advertised at http://10.1.2.3:8155/api/"),
            "{lines:?}"
        );
        // The enqueue line names the channel and the per-turn reply endpoint.
        assert!(
            lines.iter().any(|l| l.contains("enqueue")
                && l.contains("C123")
                && l.contains("reply endpoint http://10.1.2.3:8155/api/")),
            "{lines:?}"
        );
    }

    #[test]
    fn dry_run_adds_api_forward_when_no_channel() {
        let lines = dry_run_lines(&opts(None), "host");
        assert!(
            lines
                .iter()
                .any(|l| l == "kubectl -n agentos port-forward svc/agentos-api 8123:8000"),
            "no --channel -> api forward: {lines:?}"
        );
        assert!(
            lines.iter().any(|l| l.contains("slack_channel")),
            "channel placeholder when omitted: {lines:?}"
        );
    }

    #[test]
    fn message_timeout_json_marks_the_terminal_state() {
        let v = message_timeout_json();
        assert!(v["reply"].is_null(), "{v}");
        assert_eq!(v["finalized"], serde_json::json!(false));
        assert_eq!(v["timed_out"], serde_json::json!(true));
        // The reply builder's key set is a different shape (no timed_out), so a
        // consumer can discriminate the two terminal states.
        assert!(v.get("thread").is_none(), "timeout carries no thread: {v}");
    }

    #[test]
    fn message_dry_run_json_carries_the_planned_action() {
        // Explicit channel passes through verbatim.
        let v = message_dry_run_json(
            "local",
            "agentos:turns",
            Some("C123"),
            "http://localhost:8155/api/",
        );
        assert_eq!(v["dry_run"], serde_json::json!(true));
        assert_eq!(v["target"], serde_json::json!("local"));
        assert_eq!(v["stream"], serde_json::json!("agentos:turns"));
        assert_eq!(v["channel"], serde_json::json!("C123"));
        assert_eq!(
            v["reply_endpoint"],
            serde_json::json!("http://localhost:8155/api/")
        );
        // Omitted channel is JSON null, not a placeholder string.
        let v = message_dry_run_json("cluster", "s", None, "http://10.1.2.3:8155/api/");
        assert!(v["channel"].is_null(), "{v}");
        assert_eq!(v["target"], serde_json::json!("cluster"));
    }

    // --- eval parity verb ---------------------------------------------------

    use crate::evals::{EvalCase, Grader, GraderKind};

    fn eval_opts(local: bool, channel: Option<&str>) -> EvalOpts {
        EvalOpts {
            cases: None,
            channel: channel.map(str::to_string),
            namespace: "agentos".into(),
            release: "agentos".into(),
            listen_host: None,
            listen_port: DEFAULT_LISTEN_PORT,
            valkey_local_port: DEFAULT_VALKEY_LOCAL_PORT,
            valkey_password: DEFAULT_VALKEY_PASSWORD.into(),
            api_local_port: DEFAULT_API_LOCAL_PORT,
            api_key: DEFAULT_API_KEY.into(),
            user: DEFAULT_USER.into(),
            stream: DEFAULT_STREAM.into(),
            timeout_secs: DEFAULT_TIMEOUT_SECS,
            dry_run: true,
            local,
            api_url: None,
        }
    }

    fn eval_case(kind: GraderKind, expected: &str) -> EvalCase {
        EvalCase {
            id: "c1".into(),
            input: "ping".into(),
            grader: Grader {
                kind,
                expected: expected.into(),
                case_sensitive: false,
            },
        }
    }

    #[test]
    fn reply_passes_only_on_a_matching_captured_reply() {
        let case = eval_case(GraderKind::Contains, "pong");
        // Replied + grader matches -> pass; the shared Grader grades the reply.
        assert!(reply_passes(
            &case,
            &Outcome::Replied("the answer is PONG".into())
        ));
        // Replied but grader misses -> fail.
        assert!(!reply_passes(&case, &Outcome::Replied("nope".into())));
        // No reply text and no completion never pass, mirroring turn_passes.
        assert!(!reply_passes(&case, &Outcome::CompletedNoEdit));
        assert!(!reply_passes(&case, &Outcome::TimedOut));
    }

    #[test]
    fn local_eval_dry_run_plan_names_the_tier_suite_and_enqueue() {
        // The `local eval` path with no live stack: the plan is a pure render.
        let lines = eval_dry_run_lines(&eval_opts(true, Some("C123")), "smoke", 3);
        assert!(
            lines
                .iter()
                .any(|l| l == "grade 3 case(s) from suite \"smoke\" against the local tier"),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l == "enqueue onto redis redis://:valkeypass@localhost:26379"),
            "{lines:?}"
        );
        assert!(lines.iter().any(|l| l == "channel C123"), "{lines:?}");
        // No cluster plumbing (a kubectl port-forward command) leaks into the
        // local plan; the "no kubectl/helm" descriptor line is fine.
        assert!(
            !lines.iter().any(|l| l.starts_with("kubectl ")),
            "local plan has no kubectl command: {lines:?}"
        );
        assert!(
            lines.iter().any(
                |l| l.contains("enqueue one synthetic QueuedTurn per case on stream")
                    && l.contains(DEFAULT_STREAM)
            ),
            "{lines:?}"
        );
    }

    #[test]
    fn local_eval_dry_run_names_the_channel_lookup_when_omitted() {
        let lines = eval_dry_run_lines(&eval_opts(true, None), "smoke", 1);
        assert!(
            lines.iter().any(|l| l.contains("sole deployed agent")),
            "channel placeholder when omitted: {lines:?}"
        );
    }

    #[test]
    fn cluster_eval_dry_run_plan_lists_the_valkey_forward_and_stub() {
        let lines = eval_dry_run_lines(&eval_opts(false, Some("C1")), "smoke", 2);
        assert!(
            lines
                .iter()
                .any(|l| l == "grade 2 case(s) from suite \"smoke\" against the cluster tier"),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l == "kubectl -n agentos port-forward svc/agentos-valkey 56381:6379"),
            "{lines:?}"
        );
        // Explicit channel -> no api forward.
        assert!(
            !lines.iter().any(|l| l.contains("svc/agentos-api")),
            "explicit channel needs no api forward: {lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l.starts_with("stub advertised at http://")),
            "{lines:?}"
        );
    }
}
