//! `agentos message`: drive the DEPLOYED Kubernetes release end to end from the
//! CLI with zero Slack contact.
//!
//! Product rationale: a developer building an agent for someone else's Slack
//! workspace can exercise the entire deployed machinery (Valkey queue -> worker
//! -> claimed sandbox -> the real skill -> the reply) without any Slack access,
//! tokens, or workspace. It is `agentos chat`'s engine (a local Slack Web API
//! stub + the frozen `QueuedSlackEvent` enqueue + the ack-based completion
//! signal) with Kubernetes-aware auto-plumbing bolted on top:
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

use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};

use crate::api::{Agent, ApiClient};
use crate::chat::{await_reply, print_continue_hint, resolve_targets, Outcome, SlackStub};
use crate::ops::{plain, require_on_path, run_capture, run_streaming, OpsCommand};
use crate::queue::{self, connect, diagnostics, xadd, QueuedSlackEvent};

pub const DEFAULT_STREAM: &str = queue::DEFAULT_STREAM;
pub const DEFAULT_USER: &str = "U-agentos-message";
pub const DEFAULT_TIMEOUT_SECS: u64 = 300;
/// Fixed stub port so the advertised URL is deterministic; `0` picks ephemeral.
pub const DEFAULT_LISTEN_PORT: u16 = 8155;
/// Local port the Valkey port-forward binds. Chosen to dodge the compose dev
/// stack, which squats 56379.
pub const DEFAULT_VALKEY_LOCAL_PORT: u16 = 56381;
/// Local port the API port-forward binds (only used for the default-channel
/// lookup when `--channel` is omitted).
pub const DEFAULT_API_LOCAL_PORT: u16 = 8123;
/// The chart's default Valkey password (values.yaml `valkey.password`).
pub const DEFAULT_VALKEY_PASSWORD: &str = "valkeypass";
/// The chart's default platform API key (values.yaml `api.apiKey`).
pub const DEFAULT_API_KEY: &str = "agentos-dev-key";

/// In-cluster service ports the port-forwards target.
const VALKEY_REMOTE_PORT: u16 = 6379;
const API_REMOTE_PORT: u16 = 8000;

/// Options for `agentos message`, mirroring its clap flags.
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
            "no agents are deployed on the platform API; deploy one with `agentos deploy` \
             or pass --channel <id>"
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

/// Split a kubeconfig server URL into (host, port), defaulting the port from the
/// scheme when absent (`https://10.1.2.3:6443` -> `("10.1.2.3", 6443)`). Used
/// only to pick a UDP-connect target for local-IP detection, so the exact port
/// barely matters (any routable port to the same host selects the same source
/// interface).
pub fn server_host_and_port(server: &str) -> Option<(String, u16)> {
    let (default_port, rest) = if let Some(r) = server.strip_prefix("https://") {
        (443u16, r)
    } else if let Some(r) = server.strip_prefix("http://") {
        (80u16, r)
    } else {
        (443u16, server)
    };
    let authority = rest.split('/').next().unwrap_or(rest).trim();
    if authority.is_empty() {
        return None;
    }
    match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() => Some((host.to_string(), port.parse().ok()?)),
        _ => Some((authority.to_string(), default_port)),
    }
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
        "enqueue a synthetic QueuedSlackEvent for channel {channel} on stream {}",
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
    println!("+ {}", cmd.display());
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
             Is the release installed? Run `agentos up` first.",
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

/// Point the deployed worker at the stub via `helm upgrade` + rollout wait,
/// unless it is already wired. Refuses a Slack-connected release without
/// `--force-wire`.
async fn wire_worker(opts: &MessageOpts, url: &str) -> Result<()> {
    if current_worker_base_url(opts).await?.as_deref() == Some(url) {
        println!("worker already wired to {url}; skipping helm upgrade");
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
    run_streaming(&wire_upgrade_command(
        &opts.namespace,
        &opts.release,
        &opts.chart,
        url,
    ))
    .await?;
    run_streaming(&worker_rollout_status_command(
        &opts.namespace,
        &opts.release,
    ))
    .await?;
    Ok(())
}

/// The `agentos message` handler.
pub async fn message(opts: MessageOpts) -> Result<()> {
    if opts.dry_run {
        let host = opts
            .listen_host
            .clone()
            .unwrap_or_else(|| "<auto-detected-local-ip>".to_string());
        for line in dry_run_lines(&opts, &host) {
            println!("{line}");
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
    println!("slack stub listening; the worker will post to {url}");

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
    println!("routing to channel {channel}");

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
    let event = QueuedSlackEvent::synthetic(
        &channel,
        &opts.user,
        &opts.text,
        &thread_ts,
        &placeholder_ts,
    );
    let stream_id = xadd(&mut conn, &opts.stream, &event).await?;
    println!(
        "enqueued {} on {} as {stream_id}",
        event.slack_event_id, opts.stream
    );
    println!(
        "waiting up to {}s for the worker to finalize the turn...",
        opts.timeout_secs
    );

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
            println!("reply    {reply}");
            print_continue_hint("message", &channel, &thread_ts);
            Ok(())
        }
        Outcome::CompletedNoEdit => {
            println!("the worker finished the turn but never edited the placeholder");
            print_continue_hint("message", &channel, &thread_ts);
            Ok(())
        }
        Outcome::TimedOut => {
            println!(
                "TIMEOUT: the worker did not finalize within {}s. Stream diagnostics:",
                opts.timeout_secs
            );
            let diag = diagnostics(&mut conn, &opts.stream, &stream_id).await;
            println!("{diag}");
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
