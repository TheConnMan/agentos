//! `agentos up | status | down`: the operator
//! day-1 lifecycle, wrapping the Helm chart and `kubectl` the way linkerd or
//! cilium wrap theirs -- a deliberately thin CLI over the chart, which stays the
//! source of truth. Every verb shells out to the `helm`/`kubectl` binaries; the
//! CLI never re-derives what a values file already declares.
//!
//! Each verb builds its command lines as a pure function returning
//! [`OpsCommand`] vectors; the executor (or the `--dry-run` printer) consumes
//! them. That split keeps the argv construction unit-testable with no cluster
//! and gives one place to mask secrets before anything is printed.

use anyhow::{bail, Context, Result};
use tokio::process::Command;

/// One external command: the program plus its argument vector, with secret
/// argument values tagged so they can be masked in any printed form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpsCommand {
    pub program: String,
    pub args: Vec<CmdArg>,
}

/// A single argv token. `SecretSet` is a `helm --set key=value` whose value is a
/// credential: the real value is used for execution, but only a masked prefix is
/// ever printed (dry-run or the echoed command line).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CmdArg {
    Plain(String),
    SecretSet { key: String, value: String },
}

impl CmdArg {
    /// The real token passed to the process.
    fn value(&self) -> String {
        match self {
            CmdArg::Plain(s) => s.clone(),
            CmdArg::SecretSet { key, value } => format!("{key}={value}"),
        }
    }

    /// The token as shown to a human: secret values are masked.
    fn masked(&self) -> String {
        match self {
            CmdArg::Plain(s) => s.clone(),
            CmdArg::SecretSet { key, value } => format!("{key}={}", mask_secret(value)),
        }
    }
}

impl OpsCommand {
    pub(crate) fn new(program: &str, args: Vec<CmdArg>) -> Self {
        Self {
            program: program.to_string(),
            args,
        }
    }

    /// The argv tail (real values) handed to `tokio::process::Command`.
    pub fn argv(&self) -> Vec<String> {
        self.args.iter().map(CmdArg::value).collect()
    }

    /// The full shell-quoted command line with secrets masked, one line as it
    /// would be typed into a shell.
    pub fn display(&self) -> String {
        let mut parts = vec![shell_quote(&self.program)];
        for a in &self.args {
            parts.push(shell_quote(&a.masked()));
        }
        parts.join(" ")
    }
}

pub(crate) fn plain(s: impl Into<String>) -> CmdArg {
    CmdArg::Plain(s.into())
}

fn secret_set(key: &str, value: &str) -> CmdArg {
    CmdArg::SecretSet {
        key: key.to_string(),
        value: value.to_string(),
    }
}

/// Mask a secret for display: the first 8 characters, then `***`. Long enough to
/// recognise a token by its prefix (e.g. `xoxb-...`), short enough to leak
/// nothing usable.
pub fn mask_secret(value: &str) -> String {
    let shown: String = value.chars().take(8).collect();
    format!("{shown}***")
}

/// POSIX shell-quote a single token: leave it bare when it is composed only of
/// safe characters, otherwise wrap in single quotes (so `--set` keys carrying
/// `[0]` array indices print quoted, matching how they must be typed).
fn shell_quote(s: &str) -> String {
    fn is_safe(c: char) -> bool {
        c.is_ascii_alphanumeric()
            || matches!(c, '_' | '.' | '/' | ':' | '=' | '@' | ',' | '-' | '+')
    }
    if !s.is_empty() && s.chars().all(is_safe) {
        s.to_string()
    } else {
        format!("'{}'", s.replace('\'', "'\\''"))
    }
}

// ---------------------------------------------------------------------------
// Options structs (mirror the clap flags in main.rs)
// ---------------------------------------------------------------------------

/// Common flags every verb carries.
#[derive(Debug, Clone)]
pub struct CommonOpts {
    pub namespace: String,
    pub release: String,
    pub dry_run: bool,
}

pub struct UpOpts {
    pub common: CommonOpts,
    pub chart: String,
    pub no_expose: bool,
    pub set: Vec<String>,
    /// Whether `--fake-model` was passed (forces the sealed install and
    /// suppresses the fake-model warning even when the env credential is set).
    pub fake_model: bool,
    /// The model credential to install with, resolved from
    /// `AGENTOS_MODEL_CREDENTIALS`. `Some(non-empty)` enables the real model and
    /// opens egress to the provider; `None` installs sealed (fake model).
    pub credentials: Option<String>,
}

pub struct DownOpts {
    pub common: CommonOpts,
    pub yes: bool,
}

// ---------------------------------------------------------------------------
// Command builders (pure; unit-tested below)
// ---------------------------------------------------------------------------

/// Egress the runner NetworkPolicy is opened to when a real model credential is
/// installed: Anthropic's published API range over TLS. The runner policy is
/// fail-closed, so a real model call needs this allowlist entry too.
const MODEL_EGRESS_CIDR: &str = "160.79.104.0/23";
const MODEL_EGRESS_PORT: u16 = 443;

/// Resolve the model credential `up` installs with. `--fake-model` forces the
/// sealed install regardless of the environment; otherwise a non-empty
/// `AGENTOS_MODEL_CREDENTIALS` value enables the real model.
pub fn resolve_up_credentials(fake_model: bool, env_value: Option<String>) -> Option<String> {
    if fake_model {
        return None;
    }
    env_value.filter(|v| !v.is_empty())
}

/// `helm upgrade --install` for the release, exposing the UI and Langfuse on
/// node ports unless `--no-expose`, plus any pass-through `--set` values. When a
/// model credential is present it also switches the fake model off, forwards the
/// credential (masked when printed), and opens the fail-closed runner egress to
/// the model provider.
pub fn up_commands(o: &UpOpts) -> Vec<OpsCommand> {
    let mut args = vec![
        plain("upgrade"),
        plain("--install"),
        plain(&o.common.release),
        plain(&o.chart),
        plain("-n"),
        plain(&o.common.namespace),
        plain("--create-namespace"),
    ];
    if !o.no_expose {
        args.push(plain("--set"));
        args.push(plain("ui.service.type=NodePort"));
        args.push(plain("--set"));
        args.push(plain("langfuse.web.service.type=NodePort"));
    }
    if let Some(credentials) = &o.credentials {
        args.push(plain("--set"));
        args.push(plain("agentSandbox.runner.fakeModel=false"));
        args.push(plain("--set"));
        args.push(secret_set("agentSandbox.runner.credentials", credentials));
        args.push(plain("--set"));
        args.push(plain(format!(
            "security.networkPolicy.allowedEgress[0].cidr={MODEL_EGRESS_CIDR}"
        )));
        args.push(plain("--set"));
        args.push(plain(
            "security.networkPolicy.allowedEgress[0].ports[0].protocol=TCP",
        ));
        args.push(plain("--set"));
        args.push(plain(format!(
            "security.networkPolicy.allowedEgress[0].ports[0].port={MODEL_EGRESS_PORT}"
        )));
    }
    for s in &o.set {
        args.push(plain("--set"));
        args.push(plain(s));
    }
    vec![OpsCommand::new("helm", args)]
}

/// The read-only commands `agentos status` runs (and prints under `--dry-run`).
pub fn status_commands(o: &CommonOpts) -> Vec<OpsCommand> {
    vec![
        helm_status_cmd(o),
        pods_cmd(o),
        svc_cmd(o, "ui"),
        svc_cmd(o, "langfuse-web"),
        kubeconfig_host_cmd(),
    ]
}

fn helm_status_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "helm",
        vec![
            plain("status"),
            plain(&o.release),
            plain("-n"),
            plain(&o.namespace),
        ],
    )
}

fn pods_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("pods"),
            plain("-n"),
            plain(&o.namespace),
        ],
    )
}

fn svc_cmd(o: &CommonOpts, suffix: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("svc"),
            plain(format!("{}-{}", o.release, suffix)),
            plain("-n"),
            plain(&o.namespace),
            plain("-o"),
            plain("json"),
        ],
    )
}

fn kubeconfig_host_cmd() -> OpsCommand {
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

fn nodes_cmd() -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![plain("get"), plain("nodes"), plain("-o"), plain("json")],
    )
}

/// `helm uninstall` then a namespace sweep of the release and the
/// agent-sandbox-system namespace (runtime sandboxes, PVCs and job pods Helm
/// does not own).
pub fn down_commands(o: &CommonOpts) -> Vec<OpsCommand> {
    vec![
        OpsCommand::new(
            "helm",
            vec![
                plain("uninstall"),
                plain(&o.release),
                plain("-n"),
                plain(&o.namespace),
            ],
        ),
        OpsCommand::new(
            "kubectl",
            vec![
                plain("delete"),
                plain("namespace"),
                plain(&o.namespace),
                plain("agent-sandbox-system"),
                plain("--ignore-not-found"),
            ],
        ),
    ]
}

/// Parse the hostname out of a kubeconfig `cluster.server` URL
/// (`https://host:6443` -> `host`).
pub fn host_from_server_url(server: &str) -> Option<String> {
    let rest = server
        .strip_prefix("https://")
        .or_else(|| server.strip_prefix("http://"))
        .unwrap_or(server);
    let host = rest
        .split('/')
        .next()
        .unwrap_or(rest)
        .rsplit_once(':')
        .map(|(h, _)| h)
        .unwrap_or(rest);
    let host = host.trim();
    (!host.is_empty()).then(|| host.to_string())
}

// ---------------------------------------------------------------------------
// Execution
// ---------------------------------------------------------------------------

/// Fail with a clear one-line error if `bin` is not on `PATH`.
pub(crate) fn require_on_path(bin: &str) -> Result<()> {
    let found = std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(bin).is_file()))
        .unwrap_or(false);
    if found {
        Ok(())
    } else {
        bail!("`{bin}` is not on PATH; install it (or add it to PATH) and retry")
    }
}

/// Print each command line (secrets masked) and exit without running anything.
pub(crate) fn print_dry_run(cmds: &[OpsCommand]) {
    for cmd in cmds {
        println!("{}", cmd.display());
    }
}

/// Run one command with inherited stdio (so helm/kubectl output streams live),
/// echoing the masked command line first. Bails on a nonzero exit.
pub(crate) async fn run_streaming(cmd: &OpsCommand) -> Result<()> {
    println!("+ {}", cmd.display());
    let status = Command::new(&cmd.program)
        .args(cmd.argv())
        .status()
        .await
        .with_context(|| format!("failed to invoke `{}`; is it on PATH?", cmd.program))?;
    if !status.success() {
        bail!("`{}` exited with {}", cmd.program, status);
    }
    Ok(())
}

/// Run one command capturing stdout; returns (success, stdout, stderr).
pub(crate) async fn run_capture(cmd: &OpsCommand) -> Result<(bool, String, String)> {
    let output = Command::new(&cmd.program)
        .args(cmd.argv())
        .output()
        .await
        .with_context(|| format!("failed to invoke `{}`; is it on PATH?", cmd.program))?;
    Ok((
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    ))
}

// ---------------------------------------------------------------------------
// Verb handlers
// ---------------------------------------------------------------------------

pub async fn up(opts: UpOpts) -> Result<()> {
    let cmds = up_commands(&opts);
    if opts.credentials.is_some() {
        println!(
            "Real model enabled (credentials from AGENTOS_MODEL_CREDENTIALS); egress opened to the model provider."
        );
    } else if !opts.fake_model {
        println!("WARNING: no AGENTOS_MODEL_CREDENTIALS set -- installing with the fake model and sealed egress.");
        println!("         Replies will be canned. Set AGENTOS_MODEL_CREDENTIALS (an Anthropic API key) and");
        println!(
            "         re-run `agentos up` (an idempotent helm upgrade) to enable the real model."
        );
    }
    if opts.common.dry_run {
        print_dry_run(&cmds);
        return Ok(());
    }
    require_on_path("helm")?;
    for cmd in &cmds {
        run_streaming(cmd).await?;
    }
    println!("\nInstalled. Run `agentos status` for pod health and URLs.");
    Ok(())
}

pub async fn status(opts: CommonOpts) -> Result<()> {
    if opts.dry_run {
        print_dry_run(&status_commands(&opts));
        return Ok(());
    }
    require_on_path("helm")?;
    require_on_path("kubectl")?;

    // (a) Helm release state.
    let (ok, out, err) = run_capture(&helm_status_cmd(&opts)).await?;
    if ok {
        let status_line = out
            .lines()
            .find(|l| l.trim_start().starts_with("STATUS:"))
            .map(str::trim)
            .unwrap_or("STATUS: unknown");
        let revision = out
            .lines()
            .find(|l| l.trim_start().starts_with("REVISION:"))
            .map(str::trim)
            .unwrap_or("REVISION: ?");
        println!("release  {} ({}, {})", opts.release, status_line, revision);
    } else {
        println!(
            "release  {} not found ({})",
            opts.release,
            err.trim().lines().next().unwrap_or("no such release")
        );
    }

    // (b) Pod health.
    let (ok, out, _) = run_capture(&pods_cmd(&opts)).await?;
    if ok {
        print_pod_summary(&out);
    } else {
        println!(
            "pods     could not list pods in namespace {}",
            opts.namespace
        );
    }

    // (c) URL discovery.
    let host = discover_host().await;
    print_service_url(&opts, "ui", "UI", &host, true).await;
    print_service_url(&opts, "langfuse-web", "Langfuse", &host, false).await;

    Ok(())
}

pub async fn down(opts: DownOpts) -> Result<()> {
    let cmds = down_commands(&opts.common);
    if opts.common.dry_run {
        print_dry_run(&cmds);
        return Ok(());
    }
    if !opts.yes && !confirm(&opts.common)? {
        println!("aborted.");
        return Ok(());
    }
    require_on_path("helm")?;
    require_on_path("kubectl")?;

    // helm uninstall, tolerating an already-absent release.
    let uninstall = &cmds[0];
    println!("+ {}", uninstall.display());
    let (ok, out, err) = run_capture(uninstall).await?;
    if ok {
        print!("{out}");
    } else if err.contains("not found") || out.contains("not found") {
        println!("release {} already absent; continuing", opts.common.release);
    } else {
        bail!("helm uninstall failed: {}", err.trim());
    }

    // Namespace sweep (runtime artifacts Helm does not own).
    run_streaming(&cmds[1]).await?;

    println!("\nTorn down. The agents.x-k8s.io CRDs are left in place intentionally.");
    Ok(())
}

/// Read a y/N confirmation from stdin for `down` when `--yes` is absent.
fn confirm(o: &CommonOpts) -> Result<bool> {
    use std::io::Write;
    print!(
        "This uninstalls release '{}' and deletes namespaces '{}' and 'agent-sandbox-system'. Continue? [y/N] ",
        o.release, o.namespace
    );
    std::io::stdout().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .context("reading confirmation from stdin")?;
    Ok(matches!(line.trim(), "y" | "Y" | "yes" | "Yes"))
}

/// Parse `kubectl get pods` tabular output into an "N/M ready" line plus any
/// pods that are not Running/Completed, by name.
fn print_pod_summary(pods_output: &str) {
    let rows: Vec<&str> = pods_output
        .lines()
        .skip(1) // header
        .filter(|l| !l.trim().is_empty())
        .collect();
    if rows.is_empty() {
        println!("pods     none in namespace");
        return;
    }
    let mut ready = 0usize;
    let mut unhealthy: Vec<&str> = Vec::new();
    for row in &rows {
        let cols: Vec<&str> = row.split_whitespace().collect();
        let name = cols.first().copied().unwrap_or("?");
        let ready_col = cols.get(1).copied().unwrap_or("");
        let phase = cols.get(2).copied().unwrap_or("");
        // READY is "n/m": ready when the two sides match.
        let all_ready = ready_col
            .split_once('/')
            .map(|(a, b)| a == b && a != "0")
            .unwrap_or(false);
        if all_ready {
            ready += 1;
        }
        if phase != "Running" && phase != "Completed" {
            unhealthy.push(name);
        }
    }
    println!("pods     {}/{} ready", ready, rows.len());
    if !unhealthy.is_empty() {
        println!("         not ready: {}", unhealthy.join(", "));
    }
}

/// Resolve the node host: the kubeconfig cluster server hostname, falling back
/// to the first node's InternalIP.
async fn discover_host() -> String {
    if let Ok((true, out, _)) = run_capture(&kubeconfig_host_cmd()).await {
        if let Some(host) = host_from_server_url(out.trim()) {
            return host;
        }
    }
    if let Ok((true, out, _)) = run_capture(&nodes_cmd()).await {
        if let Some(ip) = node_internal_ip(&out) {
            return ip;
        }
    }
    "localhost".to_string()
}

/// First node InternalIP from `kubectl get nodes -o json`.
fn node_internal_ip(nodes_json: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(nodes_json).ok()?;
    for node in v.get("items")?.as_array()? {
        let addrs = node.get("status")?.get("addresses")?.as_array()?;
        for a in addrs {
            if a.get("type").and_then(|t| t.as_str()) == Some("InternalIP") {
                if let Some(ip) = a.get("address").and_then(|s| s.as_str()) {
                    return Some(ip.to_string());
                }
            }
        }
    }
    None
}

/// Print one service's access URL: a NodePort URL when exposed, else the
/// port-forward command to reach a ClusterIP service.
async fn print_service_url(o: &CommonOpts, suffix: &str, label: &str, host: &str, api: bool) {
    let name = format!("{}-{}", o.release, suffix);
    let (ok, out, _) = match run_capture(&svc_cmd(o, suffix)).await {
        Ok(res) => res,
        Err(_) => {
            println!("{label:9}service {name} not found");
            return;
        }
    };
    if !ok {
        println!("{label:9}service {name} not found");
        return;
    }
    let suffix_path = if api { "/?api=1" } else { "" };
    match parse_service(&out) {
        Some((svc_type, node_port, _port)) if svc_type == "NodePort" => {
            if let Some(np) = node_port {
                println!("{label:9}http://{host}:{np}{suffix_path}");
            } else {
                println!("{label:9}service {name} is NodePort but exposes no nodePort yet");
            }
        }
        Some((_, _, port)) => {
            let local = if port == 0 { 8080 } else { port };
            println!(
                "{label:9}kubectl -n {} port-forward svc/{name} {local}:{port}  then http://localhost:{local}{suffix_path}",
                o.namespace
            );
        }
        None => println!("{label:9}could not read service {name}"),
    }
}

/// From `kubectl get svc -o json`, return (type, first nodePort, first port).
fn parse_service(svc_json: &str) -> Option<(String, Option<u16>, u16)> {
    let v: serde_json::Value = serde_json::from_str(svc_json).ok()?;
    let spec = v.get("spec")?;
    let svc_type = spec.get("type").and_then(|t| t.as_str())?.to_string();
    let first_port = spec.get("ports")?.as_array()?.first()?;
    let node_port = first_port
        .get("nodePort")
        .and_then(|p| p.as_u64())
        .map(|p| p as u16);
    let port = first_port.get("port").and_then(|p| p.as_u64()).unwrap_or(0) as u16;
    Some((svc_type, node_port, port))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn common() -> CommonOpts {
        CommonOpts {
            namespace: "agentos".into(),
            release: "agentos".into(),
            dry_run: false,
        }
    }

    #[test]
    fn up_defaults_expose_ui_and_langfuse() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: false,
            set: vec![],
            fake_model: false,
            credentials: None,
        });
        assert_eq!(cmds.len(), 1);
        let line = cmds[0].display();
        assert_eq!(
            line,
            "helm upgrade --install agentos charts/agentos -n agentos --create-namespace \
             --set ui.service.type=NodePort --set langfuse.web.service.type=NodePort"
        );
    }

    #[test]
    fn up_no_expose_drops_the_nodeport_sets() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: true,
            set: vec![],
            fake_model: false,
            credentials: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("NodePort"), "{line}");
        assert!(line.ends_with("--create-namespace"), "{line}");
    }

    #[test]
    fn up_passthrough_set_is_appended_verbatim() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: true,
            set: vec!["worker.replicas=2".into(), "dispatcher.deploy=false".into()],
            fake_model: false,
            credentials: None,
        });
        let line = cmds[0].display();
        assert!(
            line.ends_with("--set worker.replicas=2 --set dispatcher.deploy=false"),
            "{line}"
        );
    }

    #[test]
    fn up_without_credentials_installs_sealed() {
        // No credential and not --fake-model: a plain install with no real-model
        // or egress sets (the fake model stays on, egress stays fail-closed).
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: false,
            set: vec![],
            fake_model: false,
            credentials: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("agentSandbox.runner.fakeModel"), "{line}");
        assert!(!line.contains("agentSandbox.runner.credentials"), "{line}");
        assert!(!line.contains("allowedEgress"), "{line}");
    }

    #[test]
    fn up_fake_model_installs_sealed_like_no_credential() {
        // --fake-model resolves to no credential, so the argv is the sealed
        // install even when the caller had a credential in the environment.
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: false,
            set: vec![],
            fake_model: true,
            credentials: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("agentSandbox.runner"), "{line}");
        assert!(!line.contains("allowedEgress"), "{line}");
    }

    #[test]
    fn up_with_credentials_enables_real_model_and_masks() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            chart: "charts/agentos".into(),
            no_expose: false,
            set: vec![],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
        });
        let line = cmds[0].display();
        assert!(
            line.contains("agentSandbox.runner.fakeModel=false"),
            "{line}"
        );
        // Credential is masked in the printed form and never leaks.
        assert!(
            line.contains("agentSandbox.runner.credentials=sk-ant-s***"),
            "{line}"
        );
        assert!(!line.contains("secretsecret"), "secret leaked: {line}");
        // Model-provider egress entry (array-index keys print single-quoted).
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=160.79.104.0/23'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].ports[0].protocol=TCP'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].ports[0].port=443'"),
            "{line}"
        );
        // The real value still reaches the executed argv.
        let argv = cmds[0].argv().join(" ");
        assert!(
            argv.contains("agentSandbox.runner.credentials=sk-ant-secretsecret"),
            "{argv}"
        );
    }

    #[test]
    fn resolve_up_credentials_reflects_env_and_fake_model() {
        // Env set, not fake: real model.
        assert_eq!(
            resolve_up_credentials(false, Some("sk-ant-x".into())).as_deref(),
            Some("sk-ant-x")
        );
        // --fake-model wins even with a credential in the environment.
        assert_eq!(resolve_up_credentials(true, Some("sk-ant-x".into())), None);
        // Empty and absent both mean sealed.
        assert_eq!(resolve_up_credentials(false, Some(String::new())), None);
        assert_eq!(resolve_up_credentials(false, None), None);
    }

    #[test]
    fn down_sweeps_release_and_sandbox_namespace() {
        let cmds = down_commands(&common());
        assert_eq!(cmds.len(), 2);
        assert_eq!(cmds[0].display(), "helm uninstall agentos -n agentos");
        assert_eq!(
            cmds[1].display(),
            "kubectl delete namespace agentos agent-sandbox-system --ignore-not-found"
        );
    }

    #[test]
    fn status_lists_the_readonly_commands() {
        let cmds = status_commands(&common());
        let lines: Vec<String> = cmds.iter().map(OpsCommand::display).collect();
        assert_eq!(lines[0], "helm status agentos -n agentos");
        assert_eq!(lines[1], "kubectl get pods -n agentos");
        assert_eq!(lines[2], "kubectl get svc agentos-ui -n agentos -o json");
        assert_eq!(
            lines[3],
            "kubectl get svc agentos-langfuse-web -n agentos -o json"
        );
        assert!(
            lines[4].starts_with("kubectl config view --minify -o "),
            "{}",
            lines[4]
        );
    }

    #[test]
    fn mask_secret_shows_eight_then_stars() {
        assert_eq!(mask_secret("xoxb-abcdefghijk"), "xoxb-abc***");
        assert_eq!(mask_secret("short"), "short***");
    }

    #[test]
    fn shell_quote_quotes_only_special_tokens() {
        assert_eq!(
            shell_quote("ui.service.type=NodePort"),
            "ui.service.type=NodePort"
        );
        assert_eq!(shell_quote("a[0]=b"), "'a[0]=b'");
        assert_eq!(shell_quote(""), "''");
    }

    #[test]
    fn host_from_server_url_strips_scheme_and_port() {
        assert_eq!(
            host_from_server_url("https://10.1.2.3:6443").as_deref(),
            Some("10.1.2.3")
        );
        assert_eq!(
            host_from_server_url("https://k3s.local:6443").as_deref(),
            Some("k3s.local")
        );
        assert_eq!(
            host_from_server_url("https://host").as_deref(),
            Some("host")
        );
        assert_eq!(host_from_server_url(""), None);
    }

    #[test]
    fn parse_service_reads_type_and_ports() {
        let json = r#"{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":31234}]}}"#;
        assert_eq!(
            parse_service(json),
            Some(("NodePort".into(), Some(31234), 80))
        );
        let cluster = r#"{"spec":{"type":"ClusterIP","ports":[{"port":3000}]}}"#;
        assert_eq!(
            parse_service(cluster),
            Some(("ClusterIP".into(), None, 3000))
        );
    }

    #[test]
    fn node_internal_ip_finds_first_internal_address() {
        let json = r#"{"items":[{"status":{"addresses":[
            {"type":"Hostname","address":"node1"},
            {"type":"InternalIP","address":"192.168.1.5"}
        ]}}]}"#;
        assert_eq!(node_internal_ip(json).as_deref(), Some("192.168.1.5"));
    }

    #[test]
    fn pod_summary_does_not_panic_on_empty() {
        // Header only: no rows.
        print_pod_summary("NAME READY STATUS RESTARTS AGE");
    }
}
