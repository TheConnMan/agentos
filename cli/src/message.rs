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
//! 3. `--wire` (the default) points the deployed worker at the stub via
//!    `helm upgrade --reuse-values --set worker.slackApiBaseUrl=<url>` and waits
//!    for the rollout; `--no-wire` instead prints the exact command and refuses
//!    to enqueue when the worker is not already wired.
//! 4. A safety guard refuses to wire a Slack-connected release (one with a
//!    dispatcher deployment) unless `--force-wire` -- otherwise the stub would
//!    hijack the real Slack replies cluster-wide.
//!
//! In the demo flow `message` runs BEFORE a real Slack workspace is connected, so
//! the guard never fires there; and the helm upgrade that connects Slack clears
//! `worker.slackApiBaseUrl` back to empty, un-wiring the stub in the same step.

use std::env;
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};

use crate::api::{Agent, ApiClient};
use crate::chat::{
    await_reply, continue_hint_line, continue_hint_long_line, resolve_targets, Outcome, SlackStub,
};
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
    /// Apply the `helm upgrade` that points the worker at the stub (default).
    /// When false, refuse to run unless the worker is already wired.
    pub wire: bool,
    /// Override the Slack-connected-release safety guard.
    pub force_wire: bool,
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

/// `helm upgrade --reuse-values --set worker.slackApiBaseUrl=<url>`: point the
/// deployed worker's Slack sink at the stub. `--reuse-values` keeps every other
/// value the release already carries.
pub fn wire_upgrade_command(namespace: &str, release: &str, chart: &str, url: &str) -> OpsCommand {
    OpsCommand::new(
        "helm",
        vec![
            plain("upgrade"),
            plain(release),
            plain(chart),
            plain("-n"),
            plain(namespace),
            plain("--reuse-values"),
            plain("--set"),
            plain(format!("worker.slackApiBaseUrl={url}")),
        ],
    )
}

/// `kubectl -n <ns> rollout status deployment/<release>-worker`: wait for the
/// re-pointed worker pods to become ready before enqueuing.
pub fn worker_rollout_status_command(namespace: &str, release: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("-n"),
            plain(namespace),
            plain("rollout"),
            plain("status"),
            plain(format!("deployment/{release}-worker")),
        ],
    )
}

/// `kubectl -n <ns> get deployment <release>-<component> -o json`.
fn deploy_get_command(namespace: &str, release: &str, component: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("-n"),
            plain(namespace),
            plain("get"),
            plain("deployment"),
            plain(format!("{release}-{component}")),
            plain("-o"),
            plain("json"),
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

/// The current `SLACK_API_BASE_URL` on the worker container, from
/// `kubectl get deployment ... -o json`. `None` when the env var is unset (the
/// worker is on real Slack) or the JSON has no worker container.
pub fn worker_slack_api_base_url(deploy_json: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(deploy_json).ok()?;
    let containers = v.pointer("/spec/template/spec/containers")?.as_array()?;
    for c in containers {
        let Some(env) = c.get("env").and_then(|e| e.as_array()) else {
            continue;
        };
        for e in env {
            if e.get("name").and_then(|n| n.as_str()) == Some("SLACK_API_BASE_URL") {
                return e
                    .get("value")
                    .and_then(|val| val.as_str())
                    .map(str::to_string);
            }
        }
    }
    None
}

/// The ordered command lines (plus the stub URL and enqueue description) that a
/// real run would execute, for `--dry-run`. Pure so the rendering is testable.
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
    cmds.push(deploy_get_command(&opts.namespace, &opts.release, "worker"));
    let url = advertised_url(advertise_host, opts.listen_port);
    if opts.wire {
        cmds.push(deploy_get_command(
            &opts.namespace,
            &opts.release,
            "dispatcher",
        ));
        cmds.push(wire_upgrade_command(
            &opts.namespace,
            &opts.release,
            &opts.chart,
            &url,
        ));
        cmds.push(worker_rollout_status_command(
            &opts.namespace,
            &opts.release,
        ));
    }
    let mut lines: Vec<String> = cmds.iter().map(OpsCommand::display).collect();
    lines.push(format!("stub advertised at {url}"));
    let channel = opts
        .channel
        .clone()
        .unwrap_or_else(|| "<the sole deployed agent's slack_channel>".to_string());
    lines.push(format!(
        "enqueue a synthetic QueuedTurn for channel {channel} on stream {}",
        opts.stream
    ));
    lines
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

/// The worker's current `SLACK_API_BASE_URL`, or an error if the deployment is
/// unreadable (the release is not installed).
async fn current_worker_base_url(opts: &MessageOpts) -> Result<Option<String>> {
    let cmd = deploy_get_command(&opts.namespace, &opts.release, "worker");
    let (ok, out, err) = run_capture(&cmd).await?;
    if !ok {
        bail!(
            "could not read the worker deployment '{}-worker' in namespace {} ({}). \
             Is the release installed? Run `agentos cluster up` first.",
            opts.release,
            opts.namespace,
            err.trim().lines().next().unwrap_or("kubectl get failed")
        );
    }
    Ok(worker_slack_api_base_url(&out))
}

/// Whether the release carries a dispatcher deployment. It renders only when
/// both Slack tokens are configured (`agentos.dispatcher.enabled`), so its
/// presence is the "this release is connected to a real Slack workspace" signal.
async fn dispatcher_exists(opts: &MessageOpts) -> Result<bool> {
    let cmd = deploy_get_command(&opts.namespace, &opts.release, "dispatcher");
    let (ok, _out, _err) = run_capture(&cmd).await?;
    Ok(ok)
}

/// Run one external command under a checklist `step`, capturing its stdio.
/// Echoes the masked command line and replays the captured output as dim
/// plumbing (both no-ops unless `--debug`, so default runs stay quiet and the
/// helm/kubectl chatter is hidden). On success the step freezes done with
/// `ok_detail`; on a nonzero exit it freezes failed, surfaces the captured
/// stderr via `ui.failure`, and bails. Mirrors `ops::run_step`, which is scoped
/// to that module.
async fn run_wire_step(
    cl: &crate::ui::Checklist,
    label: &str,
    ok_detail: &str,
    cmd: &OpsCommand,
) -> Result<()> {
    let ui = crate::ui::ui();
    ui.plumbing(&format!("+ {}", cmd.display()));
    let step = cl.step(label);
    let (ok, out, err) = run_capture(cmd).await?;
    if ok {
        step.done(ok_detail);
    } else {
        step.fail("failed");
    }
    for line in out.lines().chain(err.lines()) {
        ui.plumbing(line);
    }
    if !ok {
        let reason = err
            .lines()
            .rev()
            .map(str::trim)
            .find(|l| !l.is_empty())
            .unwrap_or("command failed");
        ui.failure(&format!("`{}` failed: {reason}", cmd.program));
        bail!("`{}` exited nonzero", cmd.program);
    }
    Ok(())
}

/// Point the deployed worker at the stub via `helm upgrade` + rollout wait,
/// unless it is already wired. Refuses a Slack-connected release without
/// `--force-wire`.
async fn wire_worker(opts: &MessageOpts, url: &str) -> Result<()> {
    let ui = crate::ui::ui();
    if current_worker_base_url(opts).await?.as_deref() == Some(url) {
        ui.note(&format!(
            "worker already wired to {url}; skipping helm upgrade"
        ));
        return Ok(());
    }
    if !opts.force_wire && dispatcher_exists(opts).await? {
        bail!(
            "release '{}' has a dispatcher deployment, so it is connected to a real Slack \
             workspace. Wiring the worker to this local stub would hijack that workspace's \
             replies cluster-wide. Re-run with --force-wire to override, or --no-wire to skip \
             wiring (and wire the worker yourself).",
            opts.release
        );
    }
    require_on_path("helm")?;
    let cl = ui.checklist();
    run_wire_step(
        &cl,
        "wiring worker to stub",
        "wired",
        &wire_upgrade_command(&opts.namespace, &opts.release, &opts.chart, url),
    )
    .await?;
    run_wire_step(
        &cl,
        "waiting for worker rollout",
        "rolled out",
        &worker_rollout_status_command(&opts.namespace, &opts.release),
    )
    .await?;
    Ok(())
}

/// The `agentos local message` handler: drive the compose stack directly.
///
/// The cluster path's self-plumbing (kubectl port-forwards, the wiring helm
/// upgrade, the dispatcher guard) is all cluster-specific, so local mode keeps
/// only the shared engine: bind the same Slack stub, enqueue the same
/// `QueuedTurn`, wait on the same XACK signal. The compose worker is
/// already running and already pointed at this stub (its `SLACK_API_BASE_URL` is
/// fixed to `http://localhost:{DEFAULT_LOCAL_STUB_PORT}/api/`), so there is
/// nothing to wire.
async fn message_local(opts: MessageOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let valkey_url = local_valkey_url(&opts.valkey_password);
    let api_base = local_api_base(opts.api_url.as_deref());

    if opts.dry_run {
        ui.payload_plain("local mode (compose stack; no kubectl/helm)");
        ui.payload_plain(&format!("enqueue onto redis {valkey_url}"));
        ui.payload_plain(&format!(
            "stub advertised at http://localhost:{DEFAULT_LOCAL_STUB_PORT}/api/"
        ));
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

    let (channel, thread_ts, placeholder_ts) =
        resolve_targets(Some(&channel), opts.thread.as_deref());
    let event = synthetic_turn(
        &channel,
        &opts.user,
        &opts.text,
        &thread_ts,
        &placeholder_ts,
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
            ui.answer(&reply);
            ui.print_tokens("\n");
            persist_and_hint(&opts, TurnVerb::Local, &channel, &thread_ts);
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            step.done("no edit");
            ui.warn("the worker finished the turn but never edited the placeholder");
            persist_and_hint(&opts, TurnVerb::Local, &channel, &thread_ts);
            Ok(())
        }
        Outcome::TimedOut => {
            step.fail(&format!("timed out after {}s", opts.timeout_secs));
            ui.note("stream diagnostics:");
            let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
            ui.note(&diag);
            std::process::exit(1);
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

    // Wire the worker to the stub (default) or verify it is already wired.
    if opts.wire {
        wire_worker(&opts, &url).await?;
    } else {
        let current = current_worker_base_url(&opts).await?;
        if current.as_deref() != Some(url.as_str()) {
            bail!(
                "the deployed worker is not wired to this stub (SLACK_API_BASE_URL={}). \
                 Re-run without --no-wire, or apply it yourself:\n  {}\n  {}",
                current.as_deref().unwrap_or("<unset>"),
                wire_upgrade_command(&opts.namespace, &opts.release, &opts.chart, &url).display(),
                worker_rollout_status_command(&opts.namespace, &opts.release).display(),
            );
        }
    }

    // Enqueue the exact event the dispatcher would produce and wait for the ack.
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
            ui.answer(&reply);
            ui.print_tokens("\n");
            persist_and_hint(&opts, TurnVerb::Cluster, &channel, &thread_ts);
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            step.done("no edit");
            ui.warn("the worker finished the turn but never edited the placeholder");
            persist_and_hint(&opts, TurnVerb::Cluster, &channel, &thread_ts);
            Ok(())
        }
        Outcome::TimedOut => {
            step.fail(&format!("timed out after {}s", opts.timeout_secs));
            ui.note("stream diagnostics:");
            let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
            ui.note(&diag);
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(name: &str, channel: &str) -> Agent {
        Agent {
            id: format!("id-{name}"),
            name: name.to_string(),
            slack_channel: channel.to_string(),
        }
    }

    fn opts(channel: Option<&str>, wire: bool) -> MessageOpts {
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
            wire,
            force_wire: false,
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
    fn wire_upgrade_command_sets_the_stub_url_with_reuse_values() {
        let cmd = wire_upgrade_command(
            "agentos",
            "agentos",
            "charts/agentos",
            "http://10.1.2.3:8155/api/",
        );
        assert_eq!(
            cmd.display(),
            "helm upgrade agentos charts/agentos -n agentos --reuse-values \
             --set worker.slackApiBaseUrl=http://10.1.2.3:8155/api/"
        );
    }

    #[test]
    fn worker_rollout_status_targets_the_worker_deployment() {
        let cmd = worker_rollout_status_command("agentos", "agentos");
        assert_eq!(
            cmd.display(),
            "kubectl -n agentos rollout status deployment/agentos-worker"
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
    fn worker_slack_api_base_url_reads_the_env_value() {
        let json = r#"{"spec":{"template":{"spec":{"containers":[
            {"name":"worker","env":[
                {"name":"AGENTOS_NAMESPACE","value":"agentos"},
                {"name":"SLACK_API_BASE_URL","value":"http://10.1.2.3:8155/api/"}
            ]}
        ]}}}}"#;
        assert_eq!(
            worker_slack_api_base_url(json).as_deref(),
            Some("http://10.1.2.3:8155/api/")
        );
    }

    #[test]
    fn worker_slack_api_base_url_is_none_when_unset() {
        let json = r#"{"spec":{"template":{"spec":{"containers":[
            {"name":"worker","env":[{"name":"AGENTOS_NAMESPACE","value":"agentos"}]}
        ]}}}}"#;
        assert_eq!(worker_slack_api_base_url(json), None);
        // No worker container / malformed -> None, not a panic.
        assert_eq!(worker_slack_api_base_url("{}"), None);
    }

    #[test]
    fn dry_run_lists_forwards_wire_and_the_enqueue_with_default_wire() {
        let lines = dry_run_lines(&opts(Some("C123"), true), "10.1.2.3");
        // Explicit channel -> no API port-forward.
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
        assert!(
            lines
                .iter()
                .any(|l| l.contains("get deployment agentos-dispatcher")),
            "wire path checks the dispatcher guard: {lines:?}"
        );
        assert!(
            lines.iter().any(|l| l
                == "helm upgrade agentos charts/agentos -n agentos --reuse-values \
                    --set worker.slackApiBaseUrl=http://10.1.2.3:8155/api/"),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l == "kubectl -n agentos rollout status deployment/agentos-worker"),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l == "stub advertised at http://10.1.2.3:8155/api/"),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l.contains("enqueue") && l.contains("C123")),
            "{lines:?}"
        );
    }

    #[test]
    fn dry_run_adds_api_forward_and_drops_wiring_when_no_channel_no_wire() {
        let lines = dry_run_lines(&opts(None, false), "host");
        assert!(
            lines
                .iter()
                .any(|l| l == "kubectl -n agentos port-forward svc/agentos-api 8123:8000"),
            "no --channel -> api forward: {lines:?}"
        );
        assert!(
            !lines.iter().any(|l| l.contains("helm upgrade")),
            "--no-wire -> no upgrade: {lines:?}"
        );
        assert!(
            !lines.iter().any(|l| l.contains("dispatcher")),
            "--no-wire -> no dispatcher guard: {lines:?}"
        );
        assert!(
            lines.iter().any(|l| l.contains("slack_channel")),
            "channel placeholder when omitted: {lines:?}"
        );
    }
}
