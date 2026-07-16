//! `agentos cluster up | cluster status | cluster down`: the operator
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
    pub env: Vec<(String, String)>,
    pub secret_env: Vec<(String, String)>,
}

/// A single argv token.
///
/// `SecretSet` is a `helm --set key=value` whose value is a credential: the real
/// value is used for execution, but only a masked prefix is ever printed (dry-run
/// or the echoed command line). Note the value still lands in the process argv --
/// acceptable only for low-sensitivity tokens that already live in a k8s Secret.
///
/// `SecretValuesFile` carries one or more secret `helm` values (dotted key ->
/// value) that must **never** reach the process table. Before execution it is
/// materialized into a private (0600) temporary values file and replaced by a
/// `-f <path>` pair (see [`OpsCommand::materialize_secret_files`]); the file is
/// removed as soon as the command finishes. This keeps the secret off `ps -ef`
/// and out of `/proc/<pid>/cmdline`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CmdArg {
    Plain(String),
    SecretSet { key: String, value: String },
    SecretValuesFile(Vec<(String, String)>),
}

impl CmdArg {
    /// The real argv token(s) passed to the process. Most args map to a single
    /// token; `SecretValuesFile` is expected to have been replaced by a `-f
    /// <path>` pair during materialization, so reaching it here (an unmaterialized
    /// secret file about to be executed) is a bug -- we emit nothing rather than
    /// risk leaking, and trip a debug assertion.
    fn value_tokens(&self) -> Vec<String> {
        match self {
            CmdArg::Plain(s) => vec![s.clone()],
            CmdArg::SecretSet { key, value } => vec![format!("{key}={value}")],
            CmdArg::SecretValuesFile(_) => {
                debug_assert!(
                    false,
                    "SecretValuesFile must be materialized before argv(); \
                     call OpsCommand::materialize_secret_files first"
                );
                Vec::new()
            }
        }
    }

    /// The token(s) as shown to a human: secret values are masked. A
    /// `SecretValuesFile` prints as `-f <secret values file: key=masked, ...>` so
    /// the operator can see which values are applied without any secret leaking.
    fn masked_tokens(&self) -> Vec<String> {
        match self {
            CmdArg::Plain(s) => vec![s.clone()],
            CmdArg::SecretSet { key, value } => vec![format!("{key}={}", mask_secret(value))],
            CmdArg::SecretValuesFile(pairs) => {
                let masked: Vec<String> = pairs
                    .iter()
                    .map(|(key, value)| format!("{key}={}", mask_secret(value)))
                    .collect();
                vec![
                    "-f".to_string(),
                    format!("<secret values file: {}>", masked.join(", ")),
                ]
            }
        }
    }
}

impl OpsCommand {
    pub(crate) fn new(program: &str, args: Vec<CmdArg>) -> Self {
        Self {
            program: program.to_string(),
            args,
            env: Vec::new(),
            secret_env: Vec::new(),
        }
    }

    pub fn with_env(mut self, env: Vec<(String, String)>) -> Self {
        self.env = env;
        self
    }

    pub fn with_secret_env(mut self, secret_env: Vec<(String, String)>) -> Self {
        self.secret_env = secret_env;
        self
    }

    /// The argv tail (real values) handed to `tokio::process::Command`. Call
    /// [`materialize_secret_files`](Self::materialize_secret_files) first when the
    /// command may carry a [`CmdArg::SecretValuesFile`], otherwise those secret
    /// values are dropped rather than executed.
    pub fn argv(&self) -> Vec<String> {
        self.args.iter().flat_map(CmdArg::value_tokens).collect()
    }

    /// The full shell-quoted command line with secrets masked, one line as it
    /// would be typed into a shell.
    pub fn display(&self) -> String {
        let mut env: Vec<String> = self
            .env
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .chain(
                self.secret_env
                    .iter()
                    .map(|(key, value)| format!("{key}={}", mask_secret(value))),
            )
            .collect();
        env.sort();
        let mut parts: Vec<String> = env.iter().map(|item| shell_quote(item)).collect();
        parts.push(shell_quote(&self.program));
        for a in &self.args {
            for token in a.masked_tokens() {
                parts.push(shell_quote(&token));
            }
        }
        parts.join(" ")
    }

    /// Materialize every [`CmdArg::SecretValuesFile`] into a private (0600)
    /// temporary values file and return an equivalent command whose secrets are
    /// delivered via `helm -f <path>` instead of the argv, plus RAII guards that
    /// delete those files when dropped (so they are cleaned up even if the helm
    /// run fails). Commands without a secret values file are returned unchanged
    /// with no guards. Hold the returned guards until the process has finished.
    pub(crate) fn materialize_secret_files(
        &self,
    ) -> Result<(OpsCommand, Vec<SecretValuesFileGuard>)> {
        let mut new_args = Vec::with_capacity(self.args.len());
        let mut guards = Vec::new();
        for a in &self.args {
            match a {
                CmdArg::SecretValuesFile(pairs) => {
                    let guard = SecretValuesFileGuard::write(pairs)?;
                    new_args.push(plain("-f"));
                    new_args.push(plain(guard.path.to_string_lossy().into_owned()));
                    guards.push(guard);
                }
                other => new_args.push(other.clone()),
            }
        }
        Ok((
            OpsCommand {
                program: self.program.clone(),
                args: new_args,
                env: self.env.clone(),
                secret_env: self.secret_env.clone(),
            },
            guards,
        ))
    }
}

/// A 0600 temporary helm values file holding secret values; deleted on drop so
/// the secret never outlives the `helm` invocation, even on error.
pub(crate) struct SecretValuesFileGuard {
    path: std::path::PathBuf,
}

impl SecretValuesFileGuard {
    /// Write `pairs` (dotted helm keys -> secret values) into a fresh 0600 temp
    /// file as nested YAML (a JSON document, which helm parses as YAML), created
    /// with restrictive permissions atomically so the secret is never briefly
    /// world-readable.
    fn write(pairs: &[(String, String)]) -> Result<Self> {
        let doc = nest_dotted_keys(pairs);
        let body = serde_json::to_vec(&doc).context("serializing secret helm values")?;

        let mut path = std::env::temp_dir();
        path.push(format!("agentos-helm-values-{}.yaml", uuid::Uuid::new_v4()));

        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts
            .open(&path)
            .with_context(|| format!("creating secret helm values file {}", path.display()))?;
        // Belt-and-suspenders on platforms where create-time mode is not honored.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
                .with_context(|| format!("securing secret helm values file {}", path.display()))?;
        }
        use std::io::Write;
        file.write_all(&body)
            .with_context(|| format!("writing secret helm values file {}", path.display()))?;
        Ok(Self { path })
    }
}

impl Drop for SecretValuesFileGuard {
    fn drop(&mut self) {
        // Best-effort cleanup; nothing actionable if the temp file is already gone.
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Expand dotted helm keys (`a.b.c=value`) into a nested JSON object suitable as
/// a helm values file. JSON is a subset of YAML, so helm parses it directly, and
/// serde handles all value escaping so a secret with YAML-special characters
/// cannot break the document.
fn nest_dotted_keys(pairs: &[(String, String)]) -> serde_json::Value {
    let mut root = serde_json::Map::new();
    for (dotted, value) in pairs {
        let parts: Vec<&str> = dotted.split('.').collect();
        let mut cursor = &mut root;
        for part in &parts[..parts.len() - 1] {
            cursor = cursor
                .entry((*part).to_string())
                .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()))
                .as_object_mut()
                .expect("dotted key prefix maps to an object");
        }
        cursor.insert(
            parts[parts.len() - 1].to_string(),
            serde_json::Value::String(value.clone()),
        );
    }
    serde_json::Value::Object(root)
}

pub(crate) fn plain(s: impl Into<String>) -> CmdArg {
    CmdArg::Plain(s.into())
}

pub(crate) fn secret_set(key: &str, value: &str) -> CmdArg {
    CmdArg::SecretSet {
        key: key.to_string(),
        value: value.to_string(),
    }
}

/// A single secret helm value delivered through a private `-f` values file
/// rather than an argv `--set`, so the value never reaches the process table.
pub(crate) fn secret_values_file(key: &str, value: &str) -> CmdArg {
    CmdArg::SecretValuesFile(vec![(key.to_string(), value.to_string())])
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
    /// Named model providers (validated against [`parse_egress_provider`]) whose
    /// API host(s) runner egress is opened to. Resolved to narrow host-route
    /// CIDRs at install time into [`resolved_egress_cidrs`]; empty means no
    /// provider egress. This is the explicit replacement for the old
    /// unconditional Anthropic carve-out (#362).
    pub allow_egress_host: Vec<String>,
    /// The single-host CIDRs the named providers resolved to, populated by [`up`]
    /// from [`resolve_provider_egress_cidrs`] (offline under `--dry-run`, so
    /// empty there). Empty in the pure argv tests. Emitted as the first
    /// `allowedEgress` entries, before any `allow_web_egress` destination.
    pub resolved_egress_cidrs: Vec<String>,
    /// Operator declared CIDRs to open runner egress to for skill or tool web
    /// access, additive to the resolved provider egress. Empty means fail closed
    /// by default.
    pub allow_web_egress: Vec<String>,
    /// Whether `--fake-model` was passed (forces the sealed install and
    /// suppresses the fake-model warning even when the env credential is set).
    pub fake_model: bool,
    /// The model credential to install with, resolved from
    /// `AGENTOS_MODEL_CREDENTIALS`. `Some(non-empty)` enables the real model;
    /// `None` installs sealed (fake model). A credential alone opens NO egress --
    /// the model stays unreachable behind the fail-closed sandbox until a
    /// provider (`allow_egress_host`) or a raw range (`allow_web_egress`) is
    /// named (#362).
    pub credentials: Option<String>,
    pub local_model: Option<String>,
    /// The shell `AGENTOS_MODEL` resolved by the caller (`None` when unset or
    /// empty), used to default `agentSandbox.runner.model` for cross-tier parity
    /// with `local up` (#361).
    pub model: Option<String>,
    /// Required chart secrets (dotted helm key -> value) the CLI supplies so a
    /// no-override install never ships the published dev defaults (see #196).
    /// Populated by [`up`] from [`resolve_generated_secrets`]; empty in the pure
    /// argv tests and whenever `--dev` keeps the chart's dev defaults. Delivered
    /// through a private 0600 `-f` values file, never the argv.
    pub secrets: Vec<(String, String)>,
    /// `--dev`: keep the chart's deterministic dev-default secrets instead of
    /// generating strong per-release randoms (the first-class dev escape hatch
    /// that replaces hand-passing `--set` for every secret).
    pub dev: bool,
}

pub struct DownOpts {
    pub common: CommonOpts,
    pub yes: bool,
}

// ---------------------------------------------------------------------------
// Command builders (pure; unit-tested below)
// ---------------------------------------------------------------------------

/// Egress port shared by every runner allowlist entry (provider + web): TLS only.
const EGRESS_TCP_PORT: u16 = 443;

/// Push the three `helm --set` args for one `security.networkPolicy.allowedEgress`
/// entry (cidr + TCP port) at `idx`. Both the model carve-out and each declared
/// web destination emit this identical shape, so they share one emitter to keep
/// the array contiguous and the argv byte-identical across sources.
fn push_egress_rule(args: &mut Vec<CmdArg>, idx: usize, cidr: &str, port: u16) {
    args.push(plain("--set"));
    args.push(plain(format!(
        "security.networkPolicy.allowedEgress[{idx}].cidr={cidr}"
    )));
    args.push(plain("--set"));
    args.push(plain(format!(
        "security.networkPolicy.allowedEgress[{idx}].ports[0].protocol=TCP"
    )));
    args.push(plain("--set"));
    args.push(plain(format!(
        "security.networkPolicy.allowedEgress[{idx}].ports[0].port={port}"
    )));
}

/// Resolve the model credential `up` installs with. `--fake-model` forces the
/// sealed install regardless of the environment; otherwise a non-empty
/// `AGENTOS_MODEL_CREDENTIALS` value enables the real model.
pub fn resolve_up_credentials(fake_model: bool, env_value: Option<String>) -> Option<String> {
    if fake_model {
        return None;
    }
    env_value.filter(|v| !v.is_empty())
}

/// The helm value key that pins the sandbox runner model in the chart.
const RUNNER_MODEL_KEY: &str = "agentSandbox.runner.model";

/// The value of the last explicit `--set agentSandbox.runner.model=VAL` in
/// `set`, if the operator passed one (last wins, matching helm precedence).
/// Helm accepts comma-joined `--set a=1,b=2`, so each element is split on `,`
/// (mirroring `operator_set_keys`) before the prefix match — a runner model
/// pinned alongside other keys is detected, and a trailing key after the model
/// assignment is not swallowed into the value.
fn explicit_runner_model(set: &[String]) -> Option<&str> {
    let prefix = format!("{RUNNER_MODEL_KEY}=");
    // `strip_prefix` returns a slice of `part` (borrowing `set`), not of
    // `prefix`, so the returned borrow outlives the temporary `prefix`.
    set.iter()
        .flat_map(|s| s.split(','))
        .filter_map(|part| part.strip_prefix(&prefix))
        .next_back()
}

/// Fail loud when the shell `AGENTOS_MODEL` and an explicit
/// `--set agentSandbox.runner.model=` disagree, so the runner model is never
/// silently ambiguous (#361).
pub fn check_runner_model_conflict(model: Option<&str>, set: &[String]) -> Result<()> {
    if let (Some(y), Some(x)) = (model, explicit_runner_model(set)) {
        if x != y {
            bail!(
                "conflicting sandbox runner model: AGENTOS_MODEL=`{y}` but \
                 `--set {RUNNER_MODEL_KEY}={x}` was also passed. Remove one so the \
                 runner model is unambiguous."
            );
        }
    }
    Ok(())
}

/// Validate every operator-supplied `--allow-web-egress` value is a real CIDR
/// (`addr/prefix`) before it is interpolated into a `helm --set` argument. A
/// value containing a comma or `=` would otherwise be split by helm into
/// multiple `--set` assignments and could overwrite the model rule at index
/// `[0]`; requiring a parseable `IpAddr` plus an in-range prefix naturally
/// rejects those (and whitespace) because they fail to parse.
pub fn validate_web_egress_cidrs(cidrs: &[String]) -> Result<()> {
    for cidr in cidrs {
        let (addr, prefix) = cidr.split_once('/').ok_or_else(|| {
            anyhow::anyhow!("`--allow-web-egress` value `{cidr}` is not a CIDR (expected addr/prefix, e.g. 10.0.0.0/8)")
        })?;
        let ip: std::net::IpAddr = addr.parse().map_err(|_| {
            anyhow::anyhow!(
                "`--allow-web-egress` value `{cidr}` has an unparseable address `{addr}`"
            )
        })?;
        let bits: u8 = prefix.parse().map_err(|_| {
            anyhow::anyhow!(
                "`--allow-web-egress` value `{cidr}` has an unparseable prefix `{prefix}`"
            )
        })?;
        let max = if ip.is_ipv4() { 32 } else { 128 };
        if bits > max {
            bail!("`--allow-web-egress` value `{cidr}` has an out-of-range prefix `/{bits}` (max /{max})");
        }
    }
    Ok(())
}

/// A CIDR is a *default route* when its prefix length is `/0` (`0.0.0.0/0`,
/// `::/0`, or any `addr/0`) -- a `/0` prefix ignores the address bits entirely
/// and matches the whole address space. Opening runner egress to such a route
/// removes the chart's default-deny internet rail. Assumes the value already
/// passed `validate_web_egress_cidrs`.
pub fn is_default_route(cidr: &str) -> bool {
    cidr.rsplit_once('/')
        .and_then(|(_, prefix)| prefix.trim().parse::<u8>().ok())
        .is_some_and(|bits| bits == 0)
}

/// The distinct rail-removal warning to emit when the web-egress allowlist
/// contains one or more default routes, or `None` when it does not. Returned as
/// a pure value (not printed here) so the warning text stays unit-testable
/// independently of the `up` handler's UI side effects.
pub fn default_route_egress_warning(cidrs: &[String]) -> Option<String> {
    let routes: Vec<&str> = cidrs
        .iter()
        .map(String::as_str)
        .filter(|c| is_default_route(c))
        .collect();
    if routes.is_empty() {
        return None;
    }
    Some(format!(
        "`--allow-web-egress` includes a default route ({}); this removes the egress rail -- the sandbox can reach the entire internet",
        routes.join(", ")
    ))
}

/// The canonical model providers `--allow-egress-host` accepts, each paired with
/// the API hostname(s) its runner must reach, in the order shown in help and
/// error text. The single source of truth for both the accepted-provider set and
/// their egress hosts, so adding a provider is a one-line edit here.
///
/// This set is deliberately limited to the providers the runner can drive
/// end-to-end today (`anthropic` via `sk-ant-` keys, `openrouter` via `sk-or-`
/// keys). Opening egress to a host the runner cannot actually talk to gives
/// false confidence, so a provider is only listed once the runner has runtime
/// support for it. When the runner gains that support for additional providers
/// (e.g. the `PROVIDER_BASE_URLS` base-URL providers zhipu/moonshot/deepseek, or
/// native OpenAI/Gemini), layer them in here at the same time so the egress
/// convenience list never advertises a provider the harness cannot use.
///
/// HOSTNAMES, never CIDRs: provider IPs rotate, so they are resolved to narrow
/// host routes at install time (see [`resolve_provider_egress_cidrs`]) instead of
/// baked into this binary where a stale literal would silently break a real model
/// call.
const EGRESS_PROVIDERS: &[(&str, &[&str])] = &[
    ("anthropic", &["api.anthropic.com"]),
    ("openrouter", &["openrouter.ai"]),
];

/// The API hostname(s) a named model provider's runner must reach, or `None`
/// when the value is not one of the known providers. Lowercase-exact only, so an
/// uppercased spelling is rejected rather than silently normalized.
pub fn provider_egress_hosts(provider: &str) -> Option<&'static [&'static str]> {
    EGRESS_PROVIDERS
        .iter()
        .find(|(n, _)| *n == provider)
        .map(|(_, hosts)| *hosts)
}

/// Validate one `--allow-egress-host` value against the known providers,
/// returning its canonical `'static` name. An unknown value is a deterministic
/// input error (exit 2 / Usage) that enumerates the accepted providers and
/// points at the `--allow-web-egress` escape hatch for arbitrary destinations.
pub fn parse_egress_provider(value: &str) -> Result<&'static str, crate::exit::CliError> {
    EGRESS_PROVIDERS
        .iter()
        .find(|(n, _)| *n == value)
        .map(|(n, _)| *n)
        .ok_or_else(|| {
            let known = EGRESS_PROVIDERS
                .iter()
                .map(|(n, _)| *n)
                .collect::<Vec<_>>()
                .join(", ");
            crate::exit::CliError::usage(format!(
                "`--allow-egress-host` value `{value}` is not a known provider (expected one of: {known})"
            ))
            .with_fix(
                "pick a named provider, or open a raw range with `--allow-web-egress <CIDR>`",
            )
        })
}

/// A resolved host address as a single-host CIDR: `/32` for IPv4, `/128` for
/// IPv6. The egress rule opens exactly that address, nothing wider.
pub fn ip_to_egress_cidr(ip: std::net::IpAddr) -> String {
    let prefix = if ip.is_ipv4() { 32 } else { 128 };
    format!("{ip}/{prefix}")
}

/// Whether a resolved provider address is safe to open a runner egress route to:
/// a globally-routable unicast address. A poisoned or split-horizon DNS answer
/// that maps a provider host to the node metadata endpoint or any internal /
/// overlay host must never mint an egress /32 -- the chart emits no
/// metadataExcept for an exact-host allow, so this predicate is the only guard.
///
/// This is a COMPREHENSIVE denylist that mirrors, by hand, the special-use
/// ranges excluded by `std`'s `Ipv4Addr::is_global`/`Ipv6Addr::is_global` --
/// those APIs are still unstable, so we cannot call them and a partial denylist
/// would give false assurance. Every non-global-unicast range is rejected,
/// including ones reachable on internal/overlay networks (CGNAT, benchmarking,
/// reserved/future) that the earlier selective list let slip through.
fn is_globally_routable_egress(ip: std::net::IpAddr) -> bool {
    use std::net::IpAddr;
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            // Reject if the address falls in ANY special-use / non-global range.
            let non_global = o[0] == 0                        // 0.0.0.0/8 "this host on this network"
                || v4.is_private()                            // 10/8, 172.16/12, 192.168/16
                || (o[0] == 100 && (o[1] & 0xc0) == 0x40)     // CGNAT 100.64.0.0/10 (RFC6598)
                || v4.is_loopback()                           // 127.0.0.0/8
                || v4.is_link_local()                         // 169.254.0.0/16 (incl. IMDS 169.254.169.254)
                || (o[0] == 192 && o[1] == 0 && o[2] == 0)    // IETF protocol assignments 192.0.0.0/24
                || v4.is_documentation()                      // 192.0.2/24, 198.51.100/24, 203.0.113/24
                || (o[0] == 192 && o[1] == 88 && o[2] == 99)  // 6to4 relay anycast 192.88.99.0/24
                || (o[0] == 198 && (o[1] & 0xfe) == 18)       // benchmarking 198.18.0.0/15 (RFC2544)
                || o[0] >= 240                                // reserved/future 240.0.0.0/4 (incl. 255.255.255.255 broadcast)
                || v4.is_multicast()                          // 224.0.0.0/4
                || v4.is_unspecified()                        // 0.0.0.0 (belt-and-suspenders; covered by o[0]==0)
                || v4.is_broadcast(); // 255.255.255.255 (belt-and-suspenders; covered by o[0]>=240)
            !non_global
        }
        IpAddr::V6(v6) => {
            if v6.is_loopback() || v6.is_unspecified() || v6.is_multicast() {
                return false;
            }
            // Map an IPv4-mapped v6 back to v4 and re-check.
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_globally_routable_egress(IpAddr::V4(v4));
            }
            let seg = v6.segments();
            let is_ula = (seg[0] & 0xfe00) == 0xfc00; // fc00::/7
            let is_link_local = (seg[0] & 0xffc0) == 0xfe80; // fe80::/10
            let is_documentation = seg[0] == 0x2001 && seg[1] == 0x0db8; // 2001:db8::/32
            !(is_ula || is_link_local || is_documentation)
        }
    }
}

/// Resolve each named provider's API host(s) to single-host egress CIDRs. The
/// resolver is injected so the pure logic (dedup, sort, empty/error handling) is
/// unit-testable without touching real DNS. An unknown provider, a resolver
/// failure, or a host that resolves to no addresses is a hard error naming the
/// host -- never a silent skip, which would leave a real model call failing
/// closed with no clue why. The result is deduplicated and sorted so the install
/// argv is stable across runs.
pub fn resolve_provider_egress_cidrs(
    providers: &[String],
    resolve: impl Fn(&str) -> std::io::Result<Vec<std::net::IpAddr>>,
) -> Result<Vec<String>> {
    let mut cidrs = Vec::new();
    for p in providers {
        let hosts = provider_egress_hosts(p)
            .ok_or_else(|| anyhow::anyhow!("unknown egress provider `{p}`"))?;
        for host in hosts {
            let ips = resolve(host)
                .with_context(|| format!("resolving egress host {host} for provider {p}"))?;
            if ips.is_empty() {
                bail!("egress host {host} (provider {p}) resolved to no addresses");
            }
            for ip in ips {
                if !is_globally_routable_egress(ip) {
                    bail!("egress host {host} (provider {p}) resolved to non-routable address {ip}; refusing to open an egress route (possible DNS poisoning or split-horizon)");
                }
                cidrs.push(ip_to_egress_cidr(ip));
            }
        }
    }
    cidrs.sort();
    cidrs.dedup();
    Ok(cidrs)
}

/// A note naming the model provider(s) whose egress `cluster up` opened, or
/// `None` when no provider was requested.
pub fn provider_egress_note(providers: &[String]) -> Option<String> {
    if providers.is_empty() {
        return None;
    }
    Some(format!(
        "real model egress opened to provider(s): {}",
        providers.join(", ")
    ))
}

/// The warning to emit when a real model credential is installed but no egress
/// was opened: the runner sandbox is fail-closed, so the model is unreachable.
/// `Some` only in that one combination (a credential present with nothing opened);
/// every other case stays silent. Names both the provider flag and the raw
/// escape hatch so the operator can fix it without reading source.
pub fn sealed_credential_warning(
    credentials_present: bool,
    any_egress_opened: bool,
) -> Option<String> {
    if credentials_present && !any_egress_opened {
        Some(
            "a real model credential is set but the sandbox is sealed -- no egress opened, so the \
             model is unreachable. Pass --allow-egress-host <anthropic|openrouter> \
             (or --allow-web-egress <CIDR>) and re-run."
                .to_string(),
        )
    } else {
        None
    }
}

/// The ordered model+egress status lines `up` prints, as (is_warning, message)
/// pairs, derived purely so every credential/egress combination is unit-tested.
/// The web-egress *count* note and the default-route warning stay in the handler
/// (they keep their own tested helpers). `any_egress_opened` folds resolved
/// provider routes, declared web egress, and (under dry-run) the intent to open.
pub fn model_egress_status_lines(
    credentials_present: bool,
    local_model: bool,
    fake_model: bool,
    providers: &[String],
    any_egress_opened: bool,
    dry_run: bool,
) -> Vec<(bool, String)> {
    let mut lines: Vec<(bool, String)> = Vec::new();
    // Past-tense provider note only on a live run; under dry-run the handler
    // prints its own "a live run resolves..." note instead.
    if !providers.is_empty() && !dry_run {
        lines.push((
            false,
            provider_egress_note(providers).expect("providers non-empty"),
        ));
        lines.push((
            false,
            "resolved provider IPs can rotate; re-run `agentos cluster up` if model calls start failing".into(),
        ));
    }
    if credentials_present {
        if let Some(w) = sealed_credential_warning(true, any_egress_opened) {
            lines.push((true, w));
        }
    } else if local_model {
        lines.push((
            false,
            "local model enabled; installing the chart inference deployment".into(),
        ));
    } else if !fake_model {
        lines.push((
            true,
            format!(
                "no AGENTOS_MODEL_CREDENTIALS set; installing with the fake model{}",
                if any_egress_opened {
                    ""
                } else {
                    " (model egress stays sealed)"
                }
            ),
        ));
        lines.push((
            false,
            "Replies will be canned. Set AGENTOS_MODEL_CREDENTIALS (an Anthropic API key) and re-run `agentos cluster up` to enable the real model.".into(),
        ));
    }
    lines
}

/// The chart secrets a bare `helm install` would otherwise render from the
/// published dev defaults in `values.yaml` (see #57): every backing-store
/// password plus the Langfuse crypto material and the first-party app keys.
/// Each entry is `(dotted helm value key, random byte length)`. `cluster up`
/// supplies a strong random for each on a fresh install so the release never
/// boots on a credential that lives in this public repo. Slack tokens and the
/// model credential are deliberately absent -- they are operator-supplied via
/// their own paths (`cluster comms`, `AGENTOS_MODEL_CREDENTIALS`), not
/// generated. `langfuse.encryptionKey` must be exactly 64 hex chars, so its 32
/// bytes are load-bearing.
const REQUIRED_SECRETS: &[(&str, usize)] = &[
    ("postgres.auth.password", 24),
    ("valkey.password", 24),
    ("clickhouse.auth.password", 24),
    ("minio.auth.rootPassword", 24),
    ("langfuse.salt", 16),
    ("langfuse.encryptionKey", 32),
    ("langfuse.nextauthSecret", 24),
    ("api.apiKey", 24),
    ("api.githubWebhookSecret", 24),
];

/// `n_bytes` of OS CSPRNG output, lowercase-hex encoded (so `2 * n_bytes`
/// chars). Hex keeps the value shell-, env- and URL-safe and satisfies every
/// backing store's charset/min-length rule, and a hex `langfuse.encryptionKey`
/// is the exact `openssl rand -hex 32` shape the chart documents.
fn random_hex(n_bytes: usize) -> Result<String> {
    use std::fmt::Write;
    let mut buf = vec![0u8; n_bytes];
    getrandom::fill(&mut buf)
        .map_err(|e| anyhow::anyhow!("OS random number generator unavailable: {e}"))?;
    let mut out = String::with_capacity(n_bytes * 2);
    for b in buf {
        let _ = write!(out, "{b:02x}");
    }
    Ok(out)
}

/// The bare value keys an operator already pinned through `--set` (so the CLI
/// leaves those to the operator rather than generating over them). Handles both
/// repeated `--set` flags and helm's comma-joined `a=1,b=2` form.
fn operator_set_keys(sets: &[String]) -> std::collections::HashSet<String> {
    let mut keys = std::collections::HashSet::new();
    for s in sets {
        for part in s.split(',') {
            if let Some((k, _)) = part.split_once('=') {
                keys.insert(k.trim().to_string());
            }
        }
    }
    keys
}

/// Read a dotted helm key (`langfuse.encryptionKey`) out of a values JSON
/// object, returning the string leaf if present.
fn lookup_dotted(values: &serde_json::Value, dotted: &str) -> Option<String> {
    let mut cursor = values;
    for part in dotted.split('.') {
        cursor = cursor.get(part)?;
    }
    cursor.as_str().map(str::to_string)
}

/// Decide which [`REQUIRED_SECRETS`] values `cluster up` supplies, and how.
///
/// - `existing` is `Some(user-supplied values JSON)` when the release already
///   exists (from `helm get values -o json`), `None` on a fresh install.
/// - An operator `--set <key>=...` for a secret always wins: we supply nothing
///   for it.
/// - Fresh install: generate a strong random for every remaining key.
/// - Existing release: re-supply exactly the value helm already recorded for a
///   key (so a `helm upgrade` never rotates a live store's credential -- the
///   chart has no `lookup`-persist yet, that is #195), and never mint a new one
///   for a key helm has no record of (leaving a pre-existing release on
///   whatever it already booted with rather than rotating it out from under a
///   running data store).
///
/// Pure and non-interactive by construction: it never reads a TTY, so a
/// non-interactive / CI `cluster up` cannot hang here.
fn resolve_generated_secrets(
    existing: Option<&serde_json::Value>,
    operator_sets: &[String],
) -> Result<Vec<(String, String)>> {
    let overridden = operator_set_keys(operator_sets);
    let mut resolved = Vec::new();
    for (key, len) in REQUIRED_SECRETS {
        if overridden.contains(*key) {
            continue;
        }
        match existing {
            Some(values) => {
                if let Some(current) = lookup_dotted(values, key) {
                    if !current.is_empty() {
                        resolved.push(((*key).to_string(), current));
                    }
                }
            }
            None => resolved.push(((*key).to_string(), random_hex(*len)?)),
        }
    }
    Ok(resolved)
}

/// `helm get values <release> -n <ns> -o json`: helm's record of the values a
/// prior install supplied. `cluster up` reads it back so an upgrade re-supplies
/// the same generated secrets instead of rotating them.
fn helm_get_values_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "helm",
        vec![
            plain("get"),
            plain("values"),
            plain(&o.release),
            plain("-n"),
            plain(&o.namespace),
            plain("-o"),
            plain("json"),
        ],
    )
}

/// The user-supplied values of an existing release, or `None` when the release
/// does not exist yet (or helm cannot reach it -- treated as a fresh install;
/// the subsequent `helm upgrade --install` surfaces any real connectivity
/// error). `helm get values` prints `null` for a release with no user values,
/// which parses to `Value::Null` and yields no reusable secrets.
async fn fetch_existing_values(o: &CommonOpts) -> Result<Option<serde_json::Value>> {
    let (ok, out, _err) = run_capture(&helm_get_values_cmd(o)).await?;
    if !ok {
        return Ok(None);
    }
    Ok(Some(
        serde_json::from_str(out.trim()).unwrap_or(serde_json::Value::Null),
    ))
}

/// `helm upgrade --install` for the release, exposing the UI and Langfuse on
/// node ports unless `--no-expose`, plus any pass-through `--set` values. When a
/// model credential is present it switches the fake model off and forwards the
/// credential (masked when printed) -- but opens NO egress on its own (#362).
/// Runner egress comes only from the resolved named-provider host routes
/// (`resolved_egress_cidrs`, first) and the declared web destinations
/// (`allow_web_egress`, after), sharing one contiguous array index.
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
    if o.dev {
        // With #195 the sealed chart auto-generates strong per-release secrets
        // by default. `--dev` must opt into the chart's deterministic published
        // defaults so local/CI stacks stay reproducible and match compose.
        args.push(plain("--set"));
        args.push(plain("security.allowDevDefaults=true"));
    }
    if !o.no_expose {
        args.push(plain("--set"));
        args.push(plain("ui.service.type=NodePort"));
        args.push(plain("--set"));
        args.push(plain("langfuse.web.service.type=NodePort"));
    }
    if let Some(model) = &o.local_model {
        args.push(plain("--set"));
        args.push(plain("inference.deploy=true"));
        args.push(plain("--set"));
        args.push(plain(format!("inference.model={model}")));
    }
    if let Some(credentials) = &o.credentials {
        args.push(plain("--set"));
        args.push(plain("agentSandbox.runner.fakeModel=false"));
        // The model credential is the one high-sensitivity value here (a live API
        // key). Deliver it through a private 0600 `-f` values file instead of an
        // argv `--set`, so it never lands in the process table where any local
        // user / EDR / crash reporter could read it via `ps -ef` or
        // `/proc/<pid>/cmdline`. `fakeModel` is not secret and stays as plain
        // `--set`. helm merges `-f` values before `--set`, so a later operator
        // `--set` still overrides, matching the prior precedence. Enabling the
        // real model opens NO egress on its own (#362) -- see the egress rules
        // below, which come only from named providers and declared web ranges.
        args.push(secret_values_file(
            "agentSandbox.runner.credentials",
            credentials,
        ));
    }
    // Egress allowlist entries share one running index so the array stays
    // contiguous no matter which source contributes: the resolved named-provider
    // host routes take the first slots (in order), then each declared web
    // destination follows. When both are empty, no `allowedEgress` entry is
    // emitted and the runner stays fail-closed.
    let mut egress_idx = 0;
    for cidr in &o.resolved_egress_cidrs {
        push_egress_rule(&mut args, egress_idx, cidr, EGRESS_TCP_PORT);
        egress_idx += 1;
    }
    for cidr in &o.allow_web_egress {
        push_egress_rule(&mut args, egress_idx, cidr, EGRESS_TCP_PORT);
        egress_idx += 1;
    }
    // The generated/reused required secrets travel through one private 0600 `-f`
    // values file (materialized at run time), so no secret reaches the process
    // table. Emitted before the passthrough `--set`s below and (like the model
    // credential above) before them in helm's precedence, so an explicit
    // operator `--set` still overrides -- though `resolve_generated_secrets`
    // already skips any key the operator pinned.
    if !o.secrets.is_empty() {
        args.push(CmdArg::SecretValuesFile(o.secrets.clone()));
    }
    // Default the sandbox runner model from the shell `AGENTOS_MODEL` for
    // cross-tier parity with `local up` (#361). Injected before the passthrough
    // `--set`s so an explicit operator `--set agentSandbox.runner.model=` keeps
    // helm precedence; suppressed when the operator already pinned it (a
    // conflicting value fails loud earlier in `up`).
    if let Some(model) = &o.model {
        if explicit_runner_model(&o.set).is_none() {
            args.push(plain("--set"));
            args.push(plain(format!("{RUNNER_MODEL_KEY}={model}")));
        }
    }
    for s in &o.set {
        args.push(plain("--set"));
        args.push(plain(s));
    }
    vec![OpsCommand::new("helm", args)]
}

/// The read-only commands `agentos cluster status` runs (and prints under `--dry-run`).
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
            plain("-o"),
            plain("json"),
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
/// (`https://host:6443` -> `host`). Delegates to the shared parser in
/// `message::split_server_url` so IPv6 and scheme/path handling stay in one place.
pub fn host_from_server_url(server: &str) -> Option<String> {
    crate::message::split_server_url(server).map(|(host, _)| host.to_string())
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

/// Run one command capturing stdout; returns (success, stdout, stderr).
pub(crate) async fn run_capture(cmd: &OpsCommand) -> Result<(bool, String, String)> {
    // Materialize any secret values into a private 0600 `-f` file so the secret
    // stays out of the argv/process table. `_secret_files` guards live until the
    // end of this function, so the temp files are removed after `helm` exits
    // (including on error paths below).
    let (cmd, _secret_files) = cmd.materialize_secret_files()?;
    let output = Command::new(&cmd.program)
        .args(cmd.argv())
        .envs(cmd.env.iter().chain(cmd.secret_env.iter()).cloned())
        .output()
        .await
        .with_context(|| format!("failed to invoke `{}`; is it on PATH?", cmd.program))?;
    Ok((
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    ))
}

/// Run one command under a checklist `step` labeled `label`, capturing its
/// stdio. Echoes the masked command line and replays the captured output as dim
/// plumbing (both no-ops unless `--debug`, so default runs stay quiet and the
/// helm/kubectl/compose chatter is hidden). On success the step freezes done
/// with `ok_detail`; on a nonzero exit it freezes failed, surfaces the captured
/// stderr via `ui.failure`, and bails. Returns captured stdout.
pub(crate) async fn run_step(
    cl: &crate::ui::Checklist,
    label: &str,
    ok_detail: &str,
    cmd: &OpsCommand,
) -> Result<String> {
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
    Ok(out)
}

// ---------------------------------------------------------------------------
// Verb handlers
// ---------------------------------------------------------------------------

pub async fn up(mut opts: UpOpts) -> Result<()> {
    let ui = crate::ui::ui();
    validate_web_egress_cidrs(&opts.allow_web_egress)
        .context("invalid --allow-web-egress value")?;
    // Fail loud (even under --dry-run) if AGENTOS_MODEL and an explicit
    // `--set agentSandbox.runner.model=` disagree (#361).
    check_runner_model_conflict(opts.model.as_deref(), &opts.set)?;
    // Each `--allow-egress-host` must name a known provider. An unknown value is
    // a usage error (exit 2) pointing at `--allow-web-egress`; the `?` carries
    // the CliError's exit class into the anyhow chain.
    for h in &opts.allow_egress_host {
        parse_egress_provider(h)?;
    }

    // Resolve the required chart secrets so a no-override `cluster up` never
    // ships the published dev defaults (#196). `--dev` keeps the chart's
    // deterministic dev defaults; otherwise a fresh install generates strong
    // per-release randoms and an upgrade re-supplies whatever helm already
    // recorded (so a live store's credential is never rotated). `--dry-run`
    // stays offline (it never touches the cluster), so it previews the fresh
    // install shape -- a live run reuses any existing release's secrets.
    if !opts.dev {
        if !opts.common.dry_run {
            require_on_path("helm")?;
        }
        let existing = if opts.common.dry_run {
            None
        } else {
            fetch_existing_values(&opts.common).await?
        };
        let fresh = existing.is_none();
        opts.secrets = resolve_generated_secrets(existing.as_ref(), &opts.set)?;
        if fresh && !opts.secrets.is_empty() && !opts.common.dry_run {
            ui.note(&format!(
                "generated strong per-release secrets for {} required chart credential(s); re-running `cluster up` reuses them",
                opts.secrets.len()
            ));
        }
    }

    // Resolve the named providers' API host(s) to narrow host-route CIDRs. This
    // is the only DNS the installer does, and it stays offline under `--dry-run`
    // (the offline invariant): dry-run previews the intent without resolving,
    // and a live run resolves and opens exactly the resolved addresses.
    if !opts.allow_egress_host.is_empty() {
        if opts.common.dry_run {
            // Name each provider's host(s) without resolving them, so the
            // preview stays offline yet shows exactly what a live run reaches.
            let named = opts
                .allow_egress_host
                .iter()
                .map(|p| match provider_egress_hosts(p) {
                    Some(hosts) if !hosts.is_empty() => format!("{p} ({})", hosts.join(", ")),
                    _ => p.clone(),
                })
                .collect::<Vec<_>>()
                .join(", ");
            ui.note(&format!(
                "a live run resolves {named} to narrow /32+/128 host routes and opens runner egress to the resolved addresses (skipped here to keep --dry-run offline)"
            ));
        } else {
            let real_resolver = |host: &str| -> std::io::Result<Vec<std::net::IpAddr>> {
                use std::net::ToSocketAddrs;
                (host, 443u16)
                    .to_socket_addrs()
                    .map(|it| it.map(|s| s.ip()).collect())
            };
            opts.resolved_egress_cidrs =
                resolve_provider_egress_cidrs(&opts.allow_egress_host, real_resolver)
                    .context("resolving named provider egress hosts")?;
        }
    }

    let cmds = up_commands(&opts);
    // Provider egress is opened iff a provider was named: on a live run
    // resolve_provider_egress_cidrs bails on an empty/failed resolution (so a
    // non-empty allow_egress_host always yields non-empty resolved_egress_cidrs),
    // and under --dry-run resolution is skipped but the intent still counts.
    let any_egress = !opts.allow_egress_host.is_empty() || !opts.allow_web_egress.is_empty();
    for (warn, msg) in model_egress_status_lines(
        opts.credentials.is_some(),
        opts.local_model.is_some(),
        opts.fake_model,
        &opts.allow_egress_host,
        any_egress,
        opts.common.dry_run,
    ) {
        if warn {
            ui.warn(&msg)
        } else {
            ui.note(&msg)
        }
    }
    if let Some(warning) = default_route_egress_warning(&opts.allow_web_egress) {
        ui.warn(&warning);
    }
    if !opts.allow_web_egress.is_empty() {
        ui.note(&format!(
            "web egress opened to {} declared destination(s)",
            opts.allow_web_egress.len()
        ));
    }
    if opts.common.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: cmds.iter().map(|cmd| cmd.display()).collect(),
        });
        return Ok(());
    }
    require_on_path("helm")?;
    let cl = ui.checklist();
    let label = format!("installing release {}", opts.common.release);
    for cmd in &cmds {
        run_step(&cl, &label, "installed", cmd).await?;
    }
    ui.payload("agentos is up");
    ui.note("Run `agentos cluster status` for pod health and URLs.");
    Ok(())
}

pub async fn status(opts: CommonOpts) -> Result<()> {
    let ui = crate::ui::ui();
    if opts.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: status_commands(&opts)
                .iter()
                .map(|cmd| cmd.display())
                .collect(),
        });
        return Ok(());
    }
    require_on_path("helm")?;
    require_on_path("kubectl")?;

    // (a) Helm release state -> a bright header line.
    let (helm_ok, helm_out, helm_err) = run_capture(&helm_status_cmd(&opts)).await?;
    let field = |name: &str, default: &str| -> String {
        helm_out
            .lines()
            .find(|l| l.trim_start().starts_with(name))
            .and_then(|l| l.split_once(':'))
            .map(|(_, v)| v.trim().to_string())
            .unwrap_or_else(|| default.to_string())
    };
    let (release_state, revision) = if helm_ok {
        (field("STATUS:", "unknown"), field("REVISION:", "?"))
    } else {
        ("not found".to_string(), "none".to_string())
    };
    ui.payload(&format!(
        "agentos · namespace {} · revision {} · {}",
        opts.namespace, revision, release_state
    ));
    if !helm_ok {
        ui.note(&format!(
            "release {} not found: {}",
            opts.release,
            helm_err.trim().lines().next().unwrap_or("no such release")
        ));
    }

    // (b) Pod health.
    let (ok, out, _) = run_capture(&pods_cmd(&opts)).await?;
    let (ready, total, unhealthy) = if ok {
        let items: Vec<serde_json::Value> = serde_json::from_str::<serde_json::Value>(&out)
            .ok()
            .and_then(|v| v.get("items").and_then(|i| i.as_array()).cloned())
            .unwrap_or_default();
        print_pod_summary(&items)
    } else {
        ui.warn(&format!(
            "could not list pods in namespace {}",
            opts.namespace
        ));
        (0, 0, Vec::new())
    };

    // (c) URL discovery.
    let host = discover_host().await;
    print_service_url(&opts, "ui", "UI", &host, true).await;
    print_service_url(&opts, "langfuse-web", "Langfuse", &host, false).await;

    // (d) Overall verdict.
    if total > 0 && ready == total && unhealthy.is_empty() {
        ui.success(&format!("healthy ({ready}/{total} pods ready)"));
    } else if total == 0 {
        ui.warn("no pods running");
    } else {
        let mut msg = format!("{ready}/{total} pods ready");
        if !unhealthy.is_empty() {
            msg.push_str(&format!("; not ready: {}", unhealthy.join(", ")));
        }
        ui.warn(&msg);
    }

    Ok(())
}

pub async fn down(opts: DownOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmds = down_commands(&opts.common);
    if opts.common.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: cmds.iter().map(|cmd| cmd.display()).collect(),
        });
        return Ok(());
    }
    ui.warn(&format!(
        "this uninstalls release '{}' and deletes namespaces '{}' and 'agent-sandbox-system'",
        opts.common.release, opts.common.namespace
    ));
    if !opts.yes && !confirm(&opts.common)? {
        ui.note("aborted");
        return Ok(());
    }
    require_on_path("helm")?;
    require_on_path("kubectl")?;

    let cl = ui.checklist();

    // helm uninstall, tolerating an already-absent release.
    let uninstall = &cmds[0];
    ui.plumbing(&format!("+ {}", uninstall.display()));
    let step = cl.step("uninstalling release");
    let (ok, out, err) = run_capture(uninstall).await?;
    let absent = !ok && (err.contains("not found") || out.contains("not found"));
    if ok {
        step.done("removed");
    } else if absent {
        step.done("already absent");
    } else {
        step.fail("failed");
    }
    for line in out.lines().chain(err.lines()) {
        ui.plumbing(line);
    }
    if !ok && !absent {
        ui.failure(&format!("helm uninstall failed: {}", err.trim()));
        bail!("helm uninstall failed");
    }

    // Namespace sweep (runtime artifacts Helm does not own).
    run_step(&cl, "sweeping namespaces", "removed", &cmds[1]).await?;

    ui.payload("agentos is down");
    ui.note("The agents.x-k8s.io CRDs are left in place intentionally.");
    Ok(())
}

/// Read a y/N confirmation from stderr/stdin for `down` when `--yes` is absent.
fn confirm(o: &CommonOpts) -> Result<bool> {
    use std::io::{IsTerminal, Write};
    // An agent (or any piped stdin) can never answer this prompt; refuse instead
    // of blocking on a read that will never complete. `--yes` is the non-interactive path.
    if !std::io::stdin().is_terminal() {
        return Err(crate::exit::CliError::usage(
            "refusing to prompt for confirmation in a non-interactive session; re-run with --yes to proceed",
        )
        .with_fix("pass --yes")
        .into());
    }
    eprint!(
        "This uninstalls release '{}' and deletes namespaces '{}' and 'agent-sandbox-system'. Continue? [y/N] ",
        o.release, o.namespace
    );
    std::io::stderr().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .context("reading confirmation from stdin")?;
    Ok(matches!(line.trim(), "y" | "Y" | "yes" | "Yes"))
}

/// Render `kubectl get pods` output as a borderless table to stdout and return
/// (ready count, steady state total, names of pods not Running) so the caller
/// can summarise overall health. Terminal and terminating pods stay visible in
/// the table but are excluded from the returned tally.
fn print_pod_summary(pods: &[serde_json::Value]) -> (usize, usize, Vec<String>) {
    let ui = crate::ui::ui();
    let mut ready = 0usize;
    let mut total = 0usize;
    let mut unhealthy: Vec<String> = Vec::new();
    let mut table_rows: Vec<Vec<String>> = Vec::new();
    for pod in pods {
        let name = pod
            .get("metadata")
            .and_then(|m| m.get("name"))
            .and_then(|n| n.as_str())
            .unwrap_or("?")
            .to_string();
        let terminating = pod
            .get("metadata")
            .and_then(|m| m.get("deletionTimestamp"))
            .is_some();
        let phase = pod
            .get("status")
            .and_then(|s| s.get("phase"))
            .and_then(|p| p.as_str())
            .unwrap_or("");
        let reason = pod
            .get("status")
            .and_then(|s| s.get("reason"))
            .and_then(|r| r.as_str())
            .unwrap_or("");
        let containers = pod
            .get("status")
            .and_then(|s| s.get("containerStatuses"))
            .and_then(|c| c.as_array());
        let (ready_n, total_m) = match containers {
            Some(cs) => {
                let m = cs.len();
                let n = cs
                    .iter()
                    .filter(|c| c.get("ready").and_then(|r| r.as_bool()) == Some(true))
                    .count();
                (n, m)
            }
            None => (0, 0),
        };
        let ready_col = format!("{ready_n}/{total_m}");
        let display_status = if terminating {
            "Terminating"
        } else if !reason.is_empty() {
            reason
        } else {
            phase
        };
        table_rows.push(vec![name.clone(), ready_col, display_status.to_string()]);
        if phase == "Succeeded" || reason == "Completed" || terminating {
            continue;
        }
        total += 1;
        let all_ready = total_m > 0 && ready_n == total_m;
        if all_ready {
            ready += 1;
        }
        if phase != "Running" {
            unhealthy.push(name);
        }
    }
    if !table_rows.is_empty() {
        ui.payload_plain(&crate::ui::table(
            &["pod", "ready", "status"],
            &table_rows,
            &[],
        ));
    }
    (ready, total, unhealthy)
}

/// Resolve a routable node host: kubeconfig cluster server hostname, falling
/// back to the first node's InternalIP. None when neither is available.
async fn resolve_node_host() -> Option<String> {
    if let Ok((true, out, _)) = run_capture(&kubeconfig_host_cmd()).await {
        if let Some(host) = host_from_server_url(out.trim()) {
            return Some(host);
        }
    }
    if let Ok((true, out, _)) = run_capture(&nodes_cmd()).await {
        if let Some(ip) = node_internal_ip(&out) {
            return Some(ip);
        }
    }
    None
}

/// Resolve the node host: the kubeconfig cluster server hostname, falling back
/// to the first node's InternalIP, then to the literal `localhost`.
async fn discover_host() -> String {
    resolve_node_host()
        .await
        .unwrap_or_else(|| "localhost".to_string())
}

/// Format an `http://host:port<path>` URL for a node, bracketing an IPv6 host
/// literal so the authority is valid (`::1` -> `[::1]`). `host` is expected
/// unbracketed (as `resolve_node_host` returns it); `path` is appended verbatim
/// (`/api`, `/?api=1`, or `""`).
fn node_http_url(host: &str, port: u16, path: &str) -> String {
    if host.contains(':') {
        format!("http://[{host}]:{port}{path}")
    } else {
        format!("http://{host}:{port}{path}")
    }
}

/// A usage error (exit 2) whose fix hint points the operator at `--api-url`,
/// the escape hatch for every UI-proxy discovery failure.
fn api_url_usage_err(msg: impl Into<String>) -> anyhow::Error {
    crate::exit::CliError::usage(msg)
        .with_fix("pass --api-url")
        .into()
}

/// Build the UI `/api` proxy base URL (`http://<host>:<ui-nodeport>/api`) from
/// the UI service JSON and a resolved node host, or an actionable usage error.
/// `cluster deploy` reaches the platform API through this proxy (the UI pod
/// serves `/api`), so it never falls back to a port-forward.
fn ui_api_url_from_parts(ui_svc_json: &str, host: Option<&str>) -> Result<String> {
    match parse_service(ui_svc_json) {
        Some((svc_type, node_port, _)) if svc_type == "NodePort" => {
            let np = node_port.ok_or_else(|| {
                api_url_usage_err(
                    "the UI service is NodePort but has not been assigned a nodePort yet; wait for the release to settle or pass --api-url to target the API directly",
                )
            })?;
            let host = host.ok_or_else(|| {
                api_url_usage_err(
                    "could not determine a node host to reach the UI /api proxy; pass --api-url to target the API directly",
                )
            })?;
            Ok(node_http_url(host, np, "/api"))
        }
        Some(_) => Err(api_url_usage_err(
            "the UI service is not NodePort-exposed (installed with --no-expose?); re-run `cluster up` without --no-expose or pass --api-url to target the API directly",
        )),
        None => Err(api_url_usage_err(
            "could not read the UI service to discover the platform API URL; pass --api-url to target the API directly",
        )),
    }
}

/// Discover the UI `/api` proxy URL for a NodePort-exposed release so
/// `cluster deploy` reaches the platform API with no port-forward.
pub async fn discover_ui_api_url(namespace: &str, release: &str) -> Result<String> {
    let common = CommonOpts {
        namespace: namespace.to_string(),
        release: release.to_string(),
        dry_run: false,
    };
    let svc_json = match run_capture(&svc_cmd(&common, "ui")).await {
        Ok((true, out, _)) => out,
        _ => {
            return Err(api_url_usage_err(format!(
                "could not read the {release}-ui service in namespace {namespace} to discover the platform API URL; pass --api-url to target the API directly"
            )))
        }
    };
    let host = resolve_node_host().await;
    ui_api_url_from_parts(&svc_json, host.as_deref())
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
    let ui = crate::ui::ui();
    let name = format!("{}-{}", o.release, suffix);
    let (ok, out, _) = match run_capture(&svc_cmd(o, suffix)).await {
        Ok(res) => res,
        Err(_) => {
            ui.kv(label, &format!("service {name} not found"));
            return;
        }
    };
    if !ok {
        ui.kv(label, &format!("service {name} not found"));
        return;
    }
    let suffix_path = api_suffix_path(api);
    // Same discovery core as `cluster observability` (#460); this formatter owns
    // the wording, so the status output stays byte-identical.
    match resolve_service_endpoint(&out, host, api) {
        ServiceEndpoint::NodePortUrl(url) => ui.kv(label, &ui.url(&url)),
        ServiceEndpoint::UnassignedNodePort => ui.kv(
            label,
            &format!("service {name} is NodePort but exposes no nodePort yet"),
        ),
        ServiceEndpoint::PortForwardHint { local, port } => ui.kv(
            label,
            &port_forward_hint_with(
                &o.namespace,
                &name,
                local,
                port,
                &ui.url(&format!("http://localhost:{local}{suffix_path}")),
            ),
        ),
        ServiceEndpoint::Unreadable => ui.kv(label, &format!("could not read service {name}")),
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

// ---------------------------------------------------------------------------
// Observability twin (issue #460).
// ---------------------------------------------------------------------------

/// Structured result of resolving one service's access endpoint.
///
/// A pure, structured value rather than a pre-formatted string: the caller owns
/// all formatting, because `cluster status`'s notes embed the service **name**
/// and its ClusterIP hint embeds **namespace + name** plus a styled `ui.url(..)`
/// mid-string. Pre-formatting that into a plain URL would break the PR#34
/// "status output visually unchanged" prior intent.
///
/// The four variants map the exact `parse_service` match arms in
/// `print_service_url`; the svc-fetch-failure / `!ok` "service not found" arms
/// stay in the async wrapper, before any JSON exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceEndpoint {
    /// NodePort-exposed: a fully built URL via `node_http_url(host, np, path)`.
    NodePortUrl(String),
    /// Type NodePort but no nodePort assigned yet.
    UnassignedNodePort,
    /// ClusterIP/other: reachable only via a port-forward.
    /// `local = if port == 0 { 8080 } else { port }`.
    PortForwardHint { local: u16, port: u16 },
    /// `parse_service` returned None (malformed/unreadable JSON).
    Unreadable,
}

/// The path suffix that selects the console's API-backed view.
fn api_suffix_path(api: bool) -> &'static str {
    if api {
        "/?api=1"
    } else {
        ""
    }
}

/// Pure discovery core shared by `cluster status` and `cluster observability`:
/// map a service's JSON + a resolved node host to a structured endpoint.
/// `api` appends the Console's `/?api=1` suffix path.
fn resolve_service_endpoint(svc_json: &str, host: &str, api: bool) -> ServiceEndpoint {
    let path = api_suffix_path(api);
    match parse_service(svc_json) {
        Some((svc_type, node_port, _)) if svc_type == "NodePort" => match node_port {
            Some(np) => ServiceEndpoint::NodePortUrl(node_http_url(host, np, path)),
            None => ServiceEndpoint::UnassignedNodePort,
        },
        Some((_, _, port)) => ServiceEndpoint::PortForwardHint {
            local: if port == 0 { 8080 } else { port },
            port,
        },
        None => ServiceEndpoint::Unreadable,
    }
}

/// The port-forward hint wording. `target` is the already-rendered URL text --
/// plain for machine payloads, styled for human output -- so the wording cannot
/// drift between the two callers.
fn port_forward_hint_with(ns: &str, name: &str, local: u16, port: u16, target: &str) -> String {
    format!("kubectl -n {ns} port-forward svc/{name} {local}:{port}  then {target}")
}

/// Plain, machine-safe hint (no ANSI): used for the observability `Endpoint.note`
/// that `--json` serializes.
fn port_forward_hint(ns: &str, name: &str, local: u16, port: u16, path: &str) -> String {
    port_forward_hint_with(
        ns,
        name,
        local,
        port,
        &format!("http://localhost:{local}{path}"),
    )
}

/// The platform API service's port (`{release}-api`, `api.service.port` in the
/// chart). Owned here so the port-forward hint carries no bare literal.
const API_SERVICE_PORT: u16 = 8000;

/// Map the UI service JSON + node host to the cluster's **API base** endpoint:
/// the UI `/api` proxy URL (the in-cluster way to reach the platform API, #360),
/// which is never browsable. Degrades to a `note` endpoint on any error.
///
/// The notes are minted here rather than borrowed from `ui_api_url_from_parts`
/// on purpose: that helper speaks `cluster deploy`'s error vocabulary, where
/// `--api-url` is a real escape hatch. `cluster observability` has no such flag,
/// so its rows must never name it. Instead the row reports the true condition
/// (`ui` service missing) or hands back an actionable port-forward for the API
/// service -- plain text, since `--json` serializes this note.
fn api_base_endpoint(
    o: &CommonOpts,
    ui_svc_json: Option<&str>,
    host: Option<&str>,
) -> crate::observability::Endpoint {
    let row = |url, note| crate::observability::Endpoint {
        name: "AgentOS API".to_string(),
        url,
        note,
        browsable: false,
    };
    let Some(ui_svc_json) = ui_svc_json else {
        return row(None, Some(format!("service {}-ui not found", o.release)));
    };
    match ui_api_url_from_parts(ui_svc_json, host) {
        Ok(url) => row(Some(url), None),
        // Any other failure -- ClusterIP / `--no-expose` (a supported install
        // mode), an unassigned nodePort, an unreadable service, or an
        // unresolvable host -- still leaves a way in: port-forward the API
        // service directly.
        Err(_) => row(
            None,
            Some(port_forward_hint(
                &o.namespace,
                &format!("{}-api", o.release),
                API_SERVICE_PORT,
                API_SERVICE_PORT,
                "",
            )),
        ),
    }
}

/// Map one release service to an observability [`Endpoint`], degrading to a
/// `note` row (never a hard failure, never a message smuggled into `url`) when
/// the service is missing, unsettled, unreadable, or reachable only by a
/// port-forward.
fn service_surface(
    o: &CommonOpts,
    suffix: &str,
    name: &str,
    svc_json: Option<&str>,
    host: Option<&str>,
    api: bool,
) -> crate::observability::Endpoint {
    let svc_name = format!("{}-{}", o.release, suffix);
    let degraded = |note: String| crate::observability::Endpoint {
        name: name.to_string(),
        url: None,
        note: Some(note),
        browsable: false,
    };
    let Some(svc_json) = svc_json else {
        return degraded(format!("service {svc_name} not found"));
    };
    let Some(host) = host else {
        return degraded(format!(
            "could not determine a node host to reach service {svc_name}"
        ));
    };
    match resolve_service_endpoint(svc_json, host, api) {
        ServiceEndpoint::NodePortUrl(url) => crate::observability::Endpoint {
            name: name.to_string(),
            url: Some(url),
            note: None,
            browsable: true,
        },
        ServiceEndpoint::UnassignedNodePort => degraded(format!(
            "service {svc_name} is NodePort but exposes no nodePort yet"
        )),
        ServiceEndpoint::PortForwardHint { local, port } => degraded(port_forward_hint(
            &o.namespace,
            &svc_name,
            local,
            port,
            api_suffix_path(api),
        )),
        ServiceEndpoint::Unreadable => degraded(format!("could not read service {svc_name}")),
    }
}

/// Fetch one release service's JSON, or None when kubectl cannot read it.
async fn fetch_service(o: &CommonOpts, suffix: &str) -> Option<String> {
    match run_capture(&svc_cmd(o, suffix)).await {
        Ok((true, out, _)) => Some(out),
        _ => None,
    }
}

/// The cluster tier's three observability surfaces (payload parity with local):
/// Console via the `ui` service, Langfuse via `langfuse-web`, and the API base
/// via the UI `/api` proxy. Degrades per endpoint; never hard-fails.
pub async fn cluster_observability_endpoints(
    opts: &CommonOpts,
) -> Vec<crate::observability::Endpoint> {
    // Deliberately `resolve_node_host()` (Option -> a degraded note), NOT
    // `cluster status`'s `discover_host()` (which fabricates `localhost` when
    // neither the kubeconfig server URL nor a node InternalIP is readable).
    // This twin's primary consumer is a coding agent reading `--json`
    // (ADR-0021/0038), and a `localhost` URL that will not resolve is worse for
    // it than an explicit note saying the host could not be determined. It also
    // matches the `resolve_node_host()`+Option pattern #360 set for every
    // URL-producing path (`discover_ui_api_url`) and the `api_base_endpoint`
    // row. `cluster status` stays human-facing and keeps its display
    // convenience.
    let (host, ui_svc, langfuse_svc) = tokio::join!(
        resolve_node_host(),
        fetch_service(opts, "ui"),
        fetch_service(opts, "langfuse-web"),
    );
    vec![
        service_surface(
            opts,
            "ui",
            "AgentOS Console",
            ui_svc.as_deref(),
            host.as_deref(),
            true,
        ),
        service_surface(
            opts,
            "langfuse-web",
            "Langfuse UI (traces / cost / evals)",
            langfuse_svc.as_deref(),
            host.as_deref(),
            false,
        ),
        api_base_endpoint(opts, ui_svc.as_deref(), host.as_deref()),
    ]
}

/// The read-only commands `agentos cluster observability` runs (and prints under
/// `--dry-run`).
///
/// A superset of what actually runs, not a 1:1 trace: `resolve_node_host` only
/// falls through to `nodes_cmd()` when `kubeconfig_host_cmd()` yields no host.
pub fn observability_commands(o: &CommonOpts) -> Vec<OpsCommand> {
    vec![
        kubeconfig_host_cmd(),
        nodes_cmd(),
        svc_cmd(o, "ui"),
        svc_cmd(o, "langfuse-web"),
    ]
}

/// `cluster observability`: resolve the release's observability surfaces with
/// the same discovery `cluster status` does, and return them for `emit`.
///
/// Agent-first: a browser is opened only when the human passes `--open`, and
/// never under `--json`.
pub async fn observability(
    opts: CommonOpts,
    open: bool,
) -> Result<crate::observability::ObservabilityOutput> {
    if opts.dry_run {
        return Ok(crate::observability::ObservabilityOutput::DryRun(
            crate::ui::DryRunPlan {
                lines: observability_commands(&opts)
                    .iter()
                    .map(|cmd| cmd.display())
                    .collect(),
            },
        ));
    }
    require_on_path("kubectl")?;
    let surfaces = cluster_observability_endpoints(&opts).await;
    let ui = crate::ui::ui();
    crate::observability::open_endpoints(&surfaces, open, ui.json()).await;
    // The cluster counterpart of the local tier's hint: stderr guidance, not
    // payload, since resolving a service says nothing about whether the release
    // is actually serving.
    ui.note("start these surfaces with `agentos cluster up` if they are unreachable");
    Ok(crate::observability::ObservabilityOutput::Surfaces(
        surfaces,
    ))
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
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
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
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("NodePort"), "{line}");
        assert!(line.ends_with("--create-namespace"), "{line}");
    }

    #[test]
    fn up_passthrough_set_is_appended_verbatim() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec!["worker.replicas=2".into(), "dispatcher.deploy=false".into()],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
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
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
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
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: true,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("agentSandbox.runner"), "{line}");
        assert!(!line.contains("allowedEgress"), "{line}");
    }

    #[test]
    fn up_with_credentials_enables_real_model_and_masks() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec!["anthropic".into()],
            resolved_egress_cidrs: vec!["192.0.2.10/32".into()],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("agentSandbox.runner.fakeModel=false"),
            "{line}"
        );
        // Credential is masked in the printed form and never leaks. It is now
        // shown as part of a `-f` secret values file, not a `--set`.
        assert!(
            line.contains("agentSandbox.runner.credentials=sk-ant-s***"),
            "{line}"
        );
        assert!(
            line.contains("-f '<secret values file:"),
            "credential should be delivered via a -f values file: {line}"
        );
        assert!(!line.contains("secretsecret"), "secret leaked: {line}");
        // Model-provider egress entry (array-index keys print single-quoted).
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=192.0.2.10/32'"),
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

        // Success criterion: the live credential must NOT reach the executed argv
        // (the process table). Instead helm gets `-f <path>` pointing at a private
        // 0600 file that carries the secret. Materialize the command the way the
        // executor does and inspect the real argv + file.
        let (materialized, guards) = cmds[0]
            .materialize_secret_files()
            .expect("materializing the secret values file");
        let argv = materialized.argv();
        let argv_joined = argv.join(" ");
        assert!(
            !argv_joined.contains("secretsecret"),
            "credential leaked into argv: {argv_joined}"
        );
        assert!(
            !argv_joined.contains("agentSandbox.runner.credentials="),
            "credential --set leaked into argv: {argv_joined}"
        );

        // A `-f <values-file>` pair is present; the file exists, is 0600, and
        // contains the real credential (as nested YAML/JSON helm can read).
        let f_pos = argv
            .iter()
            .position(|a| a == "-f")
            .expect("a -f flag in the materialized argv");
        let values_path = std::path::PathBuf::from(&argv[f_pos + 1]);
        assert!(values_path.exists(), "values file {values_path:?} missing");
        let body = std::fs::read_to_string(&values_path).expect("reading the values file");
        assert!(
            body.contains("sk-ant-secretsecret"),
            "credential missing from values file: {body}"
        );
        // It nests the dotted key correctly for helm.
        assert!(
            body.contains("agentSandbox")
                && body.contains("runner")
                && body.contains("credentials"),
            "values file is not the expected nested shape: {body}"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&values_path)
                .expect("stat values file")
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "values file must be 0600, was {mode:o}");
        }

        // The guard removes the file when dropped, so the secret never outlives
        // the helm run.
        drop(guards);
        assert!(
            !values_path.exists(),
            "values file should be deleted once the guard drops"
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
    fn with_env_stores_the_pairs() {
        let cmd =
            OpsCommand::new("docker", vec![plain("ps")]).with_env(vec![("A".into(), "1".into())]);
        assert_eq!(cmd.env, vec![("A".to_string(), "1".to_string())]);
    }

    #[test]
    fn display_renders_sorted_env_before_program() {
        let cmd = OpsCommand::new("docker", vec![plain("ps")])
            .with_env(vec![("B".into(), "2".into()), ("A".into(), "1".into())]);
        assert!(cmd.display().starts_with("A=1 "));
    }

    #[test]
    fn up_local_model_adds_inference_sets() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: Some("qwen3:4b".into()),
            model: None,
        });
        let line = cmds[0].display();
        assert!(line.contains("--set inference.deploy=true"), "{line}");
        assert!(line.contains("--set inference.model=qwen3:4b"), "{line}");
    }

    #[test]
    fn up_without_local_model_omits_inference_sets() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("inference.deploy"), "{line}");
        assert!(!line.contains("inference.model"), "{line}");
    }

    #[test]
    fn up_defaults_runner_model_from_env() {
        // AGENTOS_MODEL set, no explicit --set: inject the runner model (#361).
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: Some("z-ai/glm-5.2".into()),
        });
        let line = cmds[0].display();
        assert!(
            line.contains("agentSandbox.runner.model=z-ai/glm-5.2"),
            "{line}"
        );
    }

    #[test]
    fn up_without_env_model_omits_runner_model_set() {
        // No AGENTOS_MODEL: inject nothing, the chart default stands (#361).
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("agentSandbox.runner.model="), "{line}");
    }

    #[test]
    fn up_explicit_set_model_suppresses_env_injection() {
        // AGENTOS_MODEL set AND an explicit matching --set: the operator's set
        // already carries it, so no duplicate injection (#361).
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec!["agentSandbox.runner.model=z-ai/glm-5.2".into()],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: Some("z-ai/glm-5.2".into()),
        });
        let line = cmds[0].display();
        assert_eq!(
            line.matches("agentSandbox.runner.model=z-ai/glm-5.2")
                .count(),
            1,
            "runner model should appear exactly once (no duplicate injection): {line}"
        );
    }

    #[test]
    fn up_commands_comma_joined_explicit_suppresses_injection() {
        // The runner model pinned alongside another key in a comma-joined
        // `--set` must be detected so `up` does not inject a redundant
        // `--set agentSandbox.runner.model=<model>` on top of it (#361).
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec!["worker.replicas=2,agentSandbox.runner.model=glm".into()],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: Some("glm".into()),
        });
        let line = cmds[0].display();
        assert_eq!(
            line.matches("agentSandbox.runner.model=glm").count(),
            1,
            "runner model should appear exactly once (no duplicate injection): {line}"
        );
    }

    #[test]
    fn check_runner_model_conflict_mismatch_is_err() {
        let set = vec!["agentSandbox.runner.model=sonnet".into()];
        let err = check_runner_model_conflict(Some("glm"), &set).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("glm"), "{msg}");
        assert!(msg.contains("sonnet"), "{msg}");
    }

    #[test]
    fn check_runner_model_conflict_matching_is_ok() {
        let set = vec!["agentSandbox.runner.model=glm".into()];
        assert!(check_runner_model_conflict(Some("glm"), &set).is_ok());
    }

    #[test]
    fn check_runner_model_conflict_no_env_is_ok() {
        // No AGENTOS_MODEL: an explicit operator set stands, no conflict.
        let set = vec!["agentSandbox.runner.model=sonnet".into()];
        assert!(check_runner_model_conflict(None, &set).is_ok());
    }

    #[test]
    fn check_runner_model_conflict_no_explicit_set_is_ok() {
        // AGENTOS_MODEL set, no explicit set: nothing to conflict with.
        assert!(check_runner_model_conflict(Some("glm"), &[]).is_ok());
    }

    #[test]
    fn check_runner_model_conflict_comma_joined_detects_mismatch() {
        // Helm accepts `--set a=1,b=2`; the runner model pinned alongside another
        // key must still be detected so the conflict fails loud (#361).
        let set = vec!["worker.replicas=2,agentSandbox.runner.model=glm".into()];
        let err = check_runner_model_conflict(Some("sonnet"), &set).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("sonnet"), "{msg}");
        assert!(msg.contains("glm"), "{msg}");
    }

    #[test]
    fn check_runner_model_conflict_comma_joined_model_first_matches() {
        // The model assignment leading a comma-joined element must not swallow
        // the trailing key into its value (which would falsely report a
        // conflict); a matching model is a legitimate, non-conflicting install.
        let set = vec!["agentSandbox.runner.model=glm,worker.replicas=2".into()];
        assert!(check_runner_model_conflict(Some("glm"), &set).is_ok());
    }

    #[test]
    fn up_opens_web_egress_after_model() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec!["anthropic".into()],
            resolved_egress_cidrs: vec!["192.0.2.10/32".into()],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec!["203.0.113.0/24".into()],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=192.0.2.10/32'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[1].cidr=203.0.113.0/24'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[1].ports[0].protocol=TCP'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[1].ports[0].port=443'"),
            "{line}"
        );
    }

    #[test]
    fn up_web_egress_without_model_uses_index_zero() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec!["0.0.0.0/0".into()],
            fake_model: true,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(!line.contains("160.79.104.0/23"), "{line}");
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=0.0.0.0/0'"),
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
    }

    #[test]
    fn up_web_egress_multiple_cidrs_contiguous() {
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec!["anthropic".into()],
            resolved_egress_cidrs: vec!["192.0.2.10/32".into()],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec!["203.0.113.0/24".into(), "198.51.100.0/24".into()],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=192.0.2.10/32'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[1].cidr=203.0.113.0/24'"),
            "{line}"
        );
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[2].cidr=198.51.100.0/24'"),
            "{line}"
        );
    }

    #[test]
    fn up_no_web_egress_stays_sealed() {
        let sealed_cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let sealed_line = sealed_cmds[0].display();
        assert!(!sealed_line.contains("allowedEgress"), "{sealed_line}");

        let model_cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: false,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
            model: None,
        });
        let model_line = model_cmds[0].display();
        assert!(!model_line.contains("allowedEgress[1]"), "{model_line}");
    }

    #[test]
    fn validate_web_egress_cidrs_accepts_valid_and_rejects_bad() {
        // Valid IPv4 CIDR and both catch-all forms pass.
        assert!(validate_web_egress_cidrs(&["203.0.113.0/24".into()]).is_ok());
        assert!(validate_web_egress_cidrs(&["0.0.0.0/0".into()]).is_ok());
        assert!(validate_web_egress_cidrs(&["::/0".into()]).is_ok());

        // A value with a comma is rejected (would split into multiple --set).
        let err = validate_web_egress_cidrs(&[
            "10.0.0.0/8,security.networkPolicy.allowedEgress[0].cidr=0.0.0.0/0".into(),
        ])
        .unwrap_err()
        .to_string();
        assert!(err.contains("10.0.0.0/8,"), "{err}");

        // A value with an `=` is rejected.
        assert!(validate_web_egress_cidrs(&["10.0.0.0/8=x".into()]).is_err());

        // A bare address with no /prefix is rejected.
        assert!(validate_web_egress_cidrs(&["10.0.0.0".into()]).is_err());

        // An out-of-range prefix is rejected.
        assert!(validate_web_egress_cidrs(&["10.0.0.0/33".into()]).is_err());
    }

    #[test]
    fn default_route_egress_warning_fires_on_default_routes() {
        // The distinct rail-removal warning names the offending route and says
        // the sandbox can reach the entire internet -- for both catch-all forms
        // and for any `/0` prefix, which ignores the address bits.
        for route in ["0.0.0.0/0", "::/0", "10.0.0.0/0"] {
            let warning = default_route_egress_warning(&[route.into()])
                .unwrap_or_else(|| panic!("expected a warning for {route}"));
            assert!(warning.contains("removes the egress rail"), "{warning}");
            assert!(warning.contains("entire internet"), "{warning}");
            assert!(warning.contains(route), "{warning}");
        }

        // The offending route is called out even when mixed with scoped CIDRs.
        let warning = default_route_egress_warning(&["203.0.113.0/24".into(), "0.0.0.0/0".into()])
            .expect("expected a warning when a default route is present");
        assert!(warning.contains("0.0.0.0/0"), "{warning}");

        // No default route -> no warning (and it is distinct from the generic
        // "N declared destination(s)" note, which still fires separately).
        assert!(default_route_egress_warning(&[]).is_none());
        assert!(default_route_egress_warning(&["203.0.113.0/24".into()]).is_none());
        assert!(default_route_egress_warning(&["10.0.0.0/8".into()]).is_none());
        // A `/0`-suffixed *host* octet is not a default route (prefix is 24).
        assert!(default_route_egress_warning(&["10.0.0.10/24".into()]).is_none());
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
        assert_eq!(lines[1], "kubectl get pods -n agentos -o json");
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
    fn display_masks_secret_env_values() {
        let line = OpsCommand::new("docker", vec![plain("ps")])
            .with_secret_env(vec![(
                "SLACK_BOT_TOKEN".into(),
                "xoxb-1-secretsecret".into(),
            )])
            .display();
        assert!(line.contains("SLACK_BOT_TOKEN=xoxb-1-s***"), "{line}");
        assert!(!line.contains("secretsecret"), "secret leaked: {line}");
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
    fn host_from_server_url_parses_bracketed_ipv6() {
        assert_eq!(
            host_from_server_url("https://[::1]:6443").as_deref(),
            Some("::1")
        );
        assert_eq!(
            host_from_server_url("https://[2001:db8::1]:8443").as_deref(),
            Some("2001:db8::1")
        );
        assert_eq!(
            host_from_server_url("https://[::1]").as_deref(),
            Some("::1")
        );
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
        // No items: empty items array.
        let items: Vec<serde_json::Value> = Vec::new();
        let _ = print_pod_summary(&items);
    }

    #[test]
    fn pod_summary_excludes_completed_and_terminating() {
        let json = r#"[
            {"metadata":{"name":"api0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"api1"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"worker0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"worker1"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"dispatcher0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"ui0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"postgres0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"valkey0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"langfuse0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"otel0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"runnerold","deletionTimestamp":"2024-01-01T00:00:00Z"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"preflight0"},"status":{"phase":"Succeeded","reason":"Completed","containerStatuses":[{"ready":false,"restartCount":0}]}},
            {"metadata":{"name":"preflight1"},"status":{"phase":"Succeeded","reason":"Completed","containerStatuses":[{"ready":false,"restartCount":0}]}},
            {"metadata":{"name":"job0"},"status":{"phase":"Succeeded","containerStatuses":[{"ready":false,"restartCount":0}]}}
        ]"#;

        let items: Vec<serde_json::Value> = serde_json::from_str(json).unwrap();
        assert_eq!(print_pod_summary(&items), (10, 10, vec![]));
    }

    #[test]
    fn pod_summary_flags_genuinely_unhealthy_steady_state_pod() {
        let json = r#"[
            {"metadata":{"name":"api0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"worker0"},"status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0}]}},
            {"metadata":{"name":"dispatcher0"},"status":{"phase":"Pending","containerStatuses":[]}}
        ]"#;

        let items: Vec<serde_json::Value> = serde_json::from_str(json).unwrap();
        assert_eq!(
            print_pod_summary(&items),
            (2, 3, vec!["dispatcher0".to_string()])
        );
    }

    // -- #196: generate / reuse the required chart secrets ------------------

    #[test]
    fn random_hex_is_the_right_length_hex_and_unpredictable() {
        let a = random_hex(24).unwrap();
        let b = random_hex(24).unwrap();
        assert_eq!(a.len(), 48, "24 bytes -> 48 hex chars");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()), "{a}");
        assert_ne!(a, b, "two draws must differ");
        // The langfuse ENCRYPTION_KEY contract: exactly 64 hex chars.
        assert_eq!(random_hex(32).unwrap().len(), 64);
    }

    #[test]
    fn operator_set_keys_parses_repeated_and_comma_joined() {
        let keys = operator_set_keys(&[
            "api.apiKey=x".into(),
            "postgres.auth.password=y,valkey.password=z".into(),
        ]);
        assert!(keys.contains("api.apiKey"));
        assert!(keys.contains("postgres.auth.password"));
        assert!(keys.contains("valkey.password"));
        assert!(!keys.contains("api.githubWebhookSecret"));
    }

    #[test]
    fn lookup_dotted_navigates_nested_values() {
        let v: serde_json::Value =
            serde_json::from_str(r#"{"postgres":{"auth":{"password":"secretpw"}}}"#).unwrap();
        assert_eq!(
            lookup_dotted(&v, "postgres.auth.password").as_deref(),
            Some("secretpw")
        );
        assert_eq!(lookup_dotted(&v, "postgres.auth.missing"), None);
        assert_eq!(lookup_dotted(&serde_json::Value::Null, "api.apiKey"), None);
    }

    #[test]
    fn fresh_install_generates_every_required_secret() {
        // No existing release -> a strong random for each required key.
        let secrets = resolve_generated_secrets(None, &[]).unwrap();
        assert_eq!(secrets.len(), REQUIRED_SECRETS.len());
        for (key, _) in REQUIRED_SECRETS {
            let (_, value) = secrets
                .iter()
                .find(|(k, _)| k == key)
                .unwrap_or_else(|| panic!("missing generated secret for {key}"));
            assert!(!value.is_empty(), "{key} generated empty");
            assert!(
                value.chars().all(|c| c.is_ascii_hexdigit()),
                "{key}={value}"
            );
        }
        // encryptionKey keeps its exact 64-hex-char contract.
        let enc = secrets
            .iter()
            .find(|(k, _)| k == "langfuse.encryptionKey")
            .unwrap();
        assert_eq!(enc.1.len(), 64);
    }

    #[test]
    fn fresh_install_secrets_are_unpredictable_per_release() {
        let a = resolve_generated_secrets(None, &[]).unwrap();
        let b = resolve_generated_secrets(None, &[]).unwrap();
        assert_ne!(a, b, "each release must get its own randoms");
    }

    #[test]
    fn operator_set_secret_is_left_to_the_operator() {
        // A secret the operator pinned via --set is not generated over.
        let secrets = resolve_generated_secrets(None, &["api.apiKey=my-own-key".into()]).unwrap();
        assert!(
            !secrets.iter().any(|(k, _)| k == "api.apiKey"),
            "operator --set must win: {secrets:?}"
        );
        // Every other required secret is still generated.
        assert_eq!(secrets.len(), REQUIRED_SECRETS.len() - 1);
    }

    #[test]
    fn upgrade_reuses_recorded_secrets_and_never_rotates() {
        // helm get values shows what a prior install supplied; upgrade must
        // re-supply exactly those so a live store's credential is unchanged, and
        // must NOT mint a new value for a key with no record (leaving the
        // running release as-is rather than rotating it out from under a store).
        let existing: serde_json::Value = serde_json::from_str(
            r#"{"postgres":{"auth":{"password":"kept-pg-pw"}},"api":{"apiKey":"kept-api-key"}}"#,
        )
        .unwrap();
        let secrets = resolve_generated_secrets(Some(&existing), &[]).unwrap();
        assert_eq!(
            secrets,
            vec![
                (
                    "postgres.auth.password".to_string(),
                    "kept-pg-pw".to_string()
                ),
                ("api.apiKey".to_string(), "kept-api-key".to_string()),
            ],
            "upgrade must reuse recorded secrets and generate none: {secrets:?}"
        );
    }

    #[test]
    fn upgrade_ignores_empty_recorded_secret() {
        // An empty recorded value is not a real secret; do not re-supply it.
        let existing: serde_json::Value =
            serde_json::from_str(r#"{"valkey":{"password":""}}"#).unwrap();
        let secrets = resolve_generated_secrets(Some(&existing), &[]).unwrap();
        assert!(secrets.is_empty(), "{secrets:?}");
    }

    #[test]
    fn resolve_is_non_interactive_and_cannot_hang() {
        // The whole generate/reuse path is a pure function: no stdin, no TTY, so
        // a non-interactive / CI `cluster up` resolves secrets without blocking.
        // (Exercising it here would hang the test run if it ever read a TTY.)
        let _ = resolve_generated_secrets(None, &[]).unwrap();
        let _ = resolve_generated_secrets(Some(&serde_json::Value::Null), &[]).unwrap();
    }

    #[test]
    fn up_injects_generated_secrets_via_values_file_not_argv() {
        // Success criterion: a missing secret's generated value lands in the
        // private -f values file, never in the executed argv / process table.
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![
                ("api.apiKey".into(), "generated-api-key".into()),
                (
                    "langfuse.encryptionKey".into(),
                    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef0".into(),
                ),
            ],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        // Printed form masks the values and shows the -f secret values file.
        let line = cmds[0].display();
        assert!(line.contains("-f '<secret values file:"), "{line}");
        assert!(line.contains("api.apiKey=generate***"), "{line}");
        assert!(!line.contains("generated-api-key"), "secret leaked: {line}");

        // Materialize the way the executor does: the secret must be in the file,
        // not in argv.
        let (materialized, guards) = cmds[0].materialize_secret_files().unwrap();
        let argv = materialized.argv().join(" ");
        assert!(
            !argv.contains("generated-api-key"),
            "leaked into argv: {argv}"
        );
        assert!(
            !argv.contains("api.apiKey="),
            "secret --set leaked into argv: {argv}"
        );
        let f_pos = materialized.argv().iter().position(|a| a == "-f").unwrap();
        let path = std::path::PathBuf::from(&materialized.argv()[f_pos + 1]);
        let body = std::fs::read_to_string(&path).unwrap();
        assert!(body.contains("generated-api-key"), "{body}");
        assert!(
            body.contains("api") && body.contains("apiKey"),
            "values file is not the expected nested shape: {body}"
        );
        drop(guards);
    }

    #[test]
    fn up_without_generated_secrets_is_unchanged() {
        // The pure builder with no supplied secrets (the --dev path, and every
        // pre-#196 argv test) emits no secret values file.
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: true,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        assert!(!cmds[0].display().contains("secret values file"));
    }

    #[test]
    fn up_dev_emits_allow_dev_defaults_flag() {
        // Under --dev the operator opts into the deterministic published chart
        // credentials, so `up` must pass security.allowDevDefaults=true through
        // to helm (issue #195). Without it the sealed chart generates strong
        // random values and the dev/e2e stack would not match compose.
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: true,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("security.allowDevDefaults=true"),
            "expected --dev to emit security.allowDevDefaults=true: {line}"
        );
    }

    #[test]
    fn up_without_dev_omits_allow_dev_defaults_flag() {
        // The default (non-dev) path must NOT opt into the published defaults;
        // the sealed chart generates strong per-release credentials there.
        let cmds = up_commands(&UpOpts {
            common: common(),
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: None,
            local_model: None,
            model: None,
        });
        let line = cmds[0].display();
        assert!(
            !line.contains("security.allowDevDefaults"),
            "non-dev up must not emit security.allowDevDefaults: {line}"
        );
    }

    #[test]
    fn helm_get_values_reads_user_supplied_values_as_json() {
        let cmd = helm_get_values_cmd(&common());
        assert_eq!(cmd.display(), "helm get values agentos -n agentos -o json");
    }

    #[test]
    fn ui_api_url_nodeport_with_host_builds_proxy_url() {
        let json = r#"{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":31234}]}}"#;
        let url = ui_api_url_from_parts(json, Some("10.0.0.5")).expect("should build a proxy URL");
        assert_eq!(url, "http://10.0.0.5:31234/api");
    }

    #[test]
    fn node_http_url_brackets_ipv6_and_appends_path() {
        assert_eq!(
            node_http_url("10.0.0.5", 31234, "/api"),
            "http://10.0.0.5:31234/api"
        );
        assert_eq!(
            node_http_url("::1", 31234, "/api"),
            "http://[::1]:31234/api"
        );
        assert_eq!(
            node_http_url("node.local", 30080, "/?api=1"),
            "http://node.local:30080/?api=1"
        );
        assert_eq!(
            node_http_url("10.0.0.5", 30080, ""),
            "http://10.0.0.5:30080"
        );
    }

    #[test]
    fn ui_api_url_ipv6_host_is_bracketed() {
        let json = r#"{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":31234}]}}"#;
        let url = ui_api_url_from_parts(json, Some("::1")).expect("should build a proxy URL");
        assert_eq!(url, "http://[::1]:31234/api");
    }

    #[test]
    fn ui_api_url_nodeport_without_host_errs_mentioning_api_url() {
        let json = r#"{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":31234}]}}"#;
        let err = ui_api_url_from_parts(json, None).expect_err("a missing host must error");
        assert!(err.to_string().contains("--api-url"), "{err}");
    }

    #[test]
    fn ui_api_url_nodeport_without_assigned_nodeport_errs_mentioning_api_url() {
        let json = r#"{"spec":{"type":"NodePort","ports":[{"port":80}]}}"#;
        let err = ui_api_url_from_parts(json, Some("10.0.0.5"))
            .expect_err("an unassigned nodePort must error");
        assert!(err.to_string().contains("--api-url"), "{err}");
    }

    #[test]
    fn ui_api_url_clusterip_errs_mentioning_no_expose_and_api_url() {
        let json = r#"{"spec":{"type":"ClusterIP","ports":[{"port":80}]}}"#;
        let err = ui_api_url_from_parts(json, Some("10.0.0.5"))
            .expect_err("a non-NodePort service must error");
        let msg = err.to_string();
        assert!(msg.contains("--no-expose"), "{msg}");
        assert!(msg.contains("--api-url"), "{msg}");
    }

    #[test]
    fn ui_api_url_malformed_json_errs_mentioning_api_url() {
        let err =
            ui_api_url_from_parts("", Some("10.0.0.5")).expect_err("malformed JSON must error");
        assert!(err.to_string().contains("--api-url"), "{err}");
    }

    // -----------------------------------------------------------------------
    // Observability twin (issue #460): the pure discovery core that both
    // `cluster status` and `cluster observability` build on. Only the kubectl
    // boundary is mocked -- by feeding the service JSON strings kubectl returns.
    // -----------------------------------------------------------------------

    /// The NodePort service fixture kubectl returns for an exposed service.
    const NODEPORT_SVC: &str =
        r#"{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":31234}]}}"#;

    /// The ClusterIP service fixture kubectl returns for a `--no-expose` install.
    const CLUSTERIP_SVC: &str = r#"{"spec":{"type":"ClusterIP","ports":[{"port":3000}]}}"#;

    #[test]
    fn resolve_service_endpoint_nodeport_builds_the_node_url() {
        // api=true appends the Console's `/?api=1` suffix path.
        assert_eq!(
            resolve_service_endpoint(NODEPORT_SVC, "10.0.0.5", true),
            ServiceEndpoint::NodePortUrl("http://10.0.0.5:31234/?api=1".to_string())
        );
        // api=false yields the bare node URL -- no `?api=1`.
        assert_eq!(
            resolve_service_endpoint(NODEPORT_SVC, "10.0.0.5", false),
            ServiceEndpoint::NodePortUrl("http://10.0.0.5:31234".to_string())
        );
        // An IPv6 host is bracketed so the authority stays valid (via node_http_url).
        assert_eq!(
            resolve_service_endpoint(NODEPORT_SVC, "::1", true),
            ServiceEndpoint::NodePortUrl("http://[::1]:31234/?api=1".to_string())
        );
    }

    #[test]
    fn resolve_service_endpoint_clusterip_yields_a_port_forward_hint() {
        // ClusterIP: not node-exposed, so the caller must port-forward. The local
        // port mirrors the service port.
        let clusterip = r#"{"spec":{"type":"ClusterIP","ports":[{"port":80}]}}"#;
        assert_eq!(
            resolve_service_endpoint(clusterip, "10.0.0.5", true),
            ServiceEndpoint::PortForwardHint {
                local: 80,
                port: 80
            }
        );
        // An absent port parses as 0, which falls back to local port 8080.
        let no_port = r#"{"spec":{"type":"ClusterIP","ports":[{}]}}"#;
        assert_eq!(
            resolve_service_endpoint(no_port, "10.0.0.5", true),
            ServiceEndpoint::PortForwardHint {
                local: 8080,
                port: 0
            }
        );
    }

    #[test]
    fn resolve_service_endpoint_boundary_variants_do_not_panic() {
        // NodePort type but the nodePort is not assigned yet (release settling).
        let unassigned = r#"{"spec":{"type":"NodePort","ports":[{"port":80}]}}"#;
        assert_eq!(
            resolve_service_endpoint(unassigned, "10.0.0.5", true),
            ServiceEndpoint::UnassignedNodePort
        );
        // Malformed / empty JSON is unreadable, never a panic.
        assert_eq!(
            resolve_service_endpoint("", "10.0.0.5", true),
            ServiceEndpoint::Unreadable
        );
        assert_eq!(
            resolve_service_endpoint("{not json", "10.0.0.5", true),
            ServiceEndpoint::Unreadable
        );
        // Well-formed JSON with no spec is also unreadable.
        assert_eq!(
            resolve_service_endpoint(r#"{"metadata":{"name":"ui"}}"#, "10.0.0.5", true),
            ServiceEndpoint::Unreadable
        );
    }

    #[test]
    fn port_forward_hint_reproduces_the_status_hint_text() {
        // The exact hint `cluster status` prints today for a ClusterIP service
        // (PR#34 visual-parity guard): two spaces before `then`.
        assert_eq!(
            port_forward_hint("agentos", "agentos-ui", 80, 80, "/?api=1"),
            "kubectl -n agentos port-forward svc/agentos-ui 80:80  then http://localhost:80/?api=1"
        );
        // The 0-port fallback surfaces local 8080 while still forwarding to 0.
        assert_eq!(
            port_forward_hint("agentos", "agentos-langfuse-web", 8080, 0, ""),
            "kubectl -n agentos port-forward svc/agentos-langfuse-web 8080:0  then http://localhost:8080"
        );
    }

    #[test]
    fn api_base_endpoint_maps_ui_service_to_a_non_browsable_api_endpoint() {
        // A NodePort ui service resolves to the UI /api proxy URL (#360) and is
        // NEVER browsable -- it is an agent target, not a webapp.
        let ep = api_base_endpoint(&common(), Some(NODEPORT_SVC), Some("10.0.0.5"));
        assert_eq!(ep.name, "AgentOS API");
        assert_eq!(ep.url.as_deref(), Some("http://10.0.0.5:31234/api"));
        assert_eq!(ep.note, None);
        assert!(!ep.browsable);
    }

    #[test]
    fn api_base_endpoint_degrades_to_a_note_when_the_ui_service_is_unreadable() {
        // Unreadable ui service: degrade to a note endpoint rather than failing
        // the whole command, and never smuggle the message into `url`.
        let ep = api_base_endpoint(&common(), Some(""), Some("10.0.0.5"));
        assert_eq!(ep.name, "AgentOS API");
        assert_eq!(ep.url, None, "a degraded endpoint must not carry a url");
        assert!(
            ep.note.is_some(),
            "a degraded endpoint must explain itself in `note`"
        );
        assert!(!ep.browsable);
    }

    /// The API-base row must NEVER name `--api-url`: `cluster observability`
    /// has no such flag (only --namespace/--release/--dry-run/--open), so the
    /// hint inherited from `cluster deploy`'s error vocabulary is dead here.
    fn assert_no_api_url_hint(ep: &crate::observability::Endpoint) {
        let note = ep.note.as_deref().unwrap_or("");
        assert!(
            !note.contains("--api-url"),
            "`cluster observability` has no --api-url flag; dead hint in: {note}"
        );
    }

    #[test]
    fn api_base_endpoint_reports_a_missing_ui_service_as_not_found() {
        // Not "could not read" (the deploy-path wording): the true condition is
        // not-found, and this row must agree with the `ui` row from
        // `service_surface`.
        let ep = api_base_endpoint(&common(), None, Some("10.0.0.5"));
        assert_eq!(ep.url, None);
        assert_eq!(ep.note.as_deref(), Some("service agentos-ui not found"));
        assert!(!ep.browsable);
        assert_no_api_url_hint(&ep);
    }

    #[test]
    fn api_base_endpoint_hints_a_port_forward_for_a_clusterip_ui_service() {
        // `--no-expose` is a supported install mode, so this is a real path,
        // not an error: hand back an actionable port-forward for the API
        // service instead of deploy's dead --api-url hint.
        let ep = api_base_endpoint(&common(), Some(CLUSTERIP_SVC), Some("10.0.0.5"));
        assert_eq!(ep.url, None);
        assert_eq!(
            ep.note.as_deref(),
            Some("kubectl -n agentos port-forward svc/agentos-api 8000:8000  then http://localhost:8000")
        );
        assert!(!ep.browsable);
        assert_no_api_url_hint(&ep);
    }

    #[test]
    fn api_base_endpoint_notes_stay_plain_for_the_json_payload() {
        // `Ui::emit_json` documents the payload as machine-consumed: no ANSI.
        for ep in [
            api_base_endpoint(&common(), None, Some("10.0.0.5")),
            api_base_endpoint(&common(), Some(CLUSTERIP_SVC), Some("10.0.0.5")),
            api_base_endpoint(&common(), Some(""), Some("10.0.0.5")),
            api_base_endpoint(&common(), Some(NODEPORT_SVC), None),
        ] {
            let note = ep.note.as_deref().unwrap_or("");
            assert!(
                !note.contains('\u{1b}'),
                "note must carry no ANSI: {note:?}"
            );
            assert_no_api_url_hint(&ep);
        }
    }

    #[test]
    fn api_base_endpoint_hints_a_port_forward_when_the_host_is_unresolvable() {
        let ep = api_base_endpoint(&common(), Some(NODEPORT_SVC), None);
        assert_eq!(ep.url, None);
        assert_eq!(
            ep.note.as_deref(),
            Some("kubectl -n agentos port-forward svc/agentos-api 8000:8000  then http://localhost:8000")
        );
        assert!(!ep.browsable);
        assert_no_api_url_hint(&ep);
    }

    // ---- service_surface: the whole cluster-tier ServiceEndpoint -> Endpoint
    // mapper. It decides url-vs-note and owns `browsable`, the --open gate.

    #[test]
    fn service_surface_maps_a_nodeport_service_to_a_browsable_url_row() {
        let ep = service_surface(
            &common(),
            "ui",
            "AgentOS Console",
            Some(NODEPORT_SVC),
            Some("10.0.0.5"),
            true,
        );
        assert_eq!(ep.name, "AgentOS Console");
        assert_eq!(ep.url.as_deref(), Some("http://10.0.0.5:31234/?api=1"));
        assert_eq!(ep.note, None);
        assert!(ep.browsable, "a resolved NodePort URL is the --open target");
    }

    #[test]
    fn service_surface_degrades_when_the_service_is_not_found() {
        let ep = service_surface(
            &common(),
            "ui",
            "AgentOS Console",
            None,
            Some("10.0.0.5"),
            true,
        );
        assert_eq!(ep.url, None, "a degraded row must never carry a url");
        assert_eq!(ep.note.as_deref(), Some("service agentos-ui not found"));
        assert!(!ep.browsable, "--open must not fire on a degraded row");
    }

    #[test]
    fn service_surface_degrades_when_the_node_host_is_unresolvable() {
        // Pins the deliberate divergence from `cluster status`: this twin does
        // NOT inherit `discover_host()`'s `localhost` fallback, so an
        // unresolvable host is an explicit note, never a fabricated URL.
        let ep = service_surface(
            &common(),
            "ui",
            "AgentOS Console",
            Some(NODEPORT_SVC),
            None,
            true,
        );
        assert_eq!(ep.url, None, "must not fabricate a localhost URL");
        assert_eq!(
            ep.note.as_deref(),
            Some("could not determine a node host to reach service agentos-ui")
        );
        assert!(!ep.browsable);
    }

    #[test]
    fn service_surface_degrades_an_unassigned_nodeport_to_a_note() {
        let unassigned = r#"{"spec":{"type":"NodePort","ports":[{"port":80}]}}"#;
        let ep = service_surface(
            &common(),
            "ui",
            "AgentOS Console",
            Some(unassigned),
            Some("10.0.0.5"),
            true,
        );
        assert_eq!(ep.url, None);
        assert_eq!(
            ep.note.as_deref(),
            Some("service agentos-ui is NodePort but exposes no nodePort yet")
        );
        assert!(!ep.browsable);
    }

    #[test]
    fn service_surface_maps_a_clusterip_service_to_a_plain_port_forward_note() {
        let ep = service_surface(
            &common(),
            "langfuse-web",
            "Langfuse UI",
            Some(CLUSTERIP_SVC),
            Some("10.0.0.5"),
            false,
        );
        assert_eq!(ep.url, None);
        let note = ep.note.as_deref().expect("a port-forward hint");
        assert_eq!(
            note,
            "kubectl -n agentos port-forward svc/agentos-langfuse-web 3000:3000  then http://localhost:3000"
        );
        // Serialized into the --json payload, which is machine-consumed.
        assert!(
            !note.contains('\u{1b}'),
            "note must carry no ANSI: {note:?}"
        );
        assert!(!ep.browsable, "a port-forward row is not a browser target");
    }

    #[test]
    fn service_surface_degrades_an_unreadable_service_to_a_note() {
        let ep = service_surface(
            &common(),
            "ui",
            "AgentOS Console",
            Some("{not json"),
            Some("10.0.0.5"),
            true,
        );
        assert_eq!(ep.url, None);
        assert_eq!(
            ep.note.as_deref(),
            Some("could not read service agentos-ui")
        );
        assert!(!ep.browsable);
    }

    #[test]
    fn observability_dry_run_plan_lists_the_read_only_lookups() {
        let lines: Vec<String> = observability_commands(&common())
            .iter()
            .map(|c| c.display())
            .collect();
        assert_eq!(lines.len(), 4, "{lines:?}");
        assert!(
            lines.iter().any(|l| l.contains("get svc agentos-ui")),
            "{lines:?}"
        );
        assert!(
            lines
                .iter()
                .any(|l| l.contains("get svc agentos-langfuse-web")),
            "{lines:?}"
        );
    }

    // -----------------------------------------------------------------------
    // Explicit provider egress (issue #362): the model-provider carve-out is no
    // longer a hardcoded Anthropic CIDR pushed whenever a credential is present;
    // egress is opened only for operator-named providers, resolved to their API
    // host IPs, so a real model call fails closed unless the provider is asked
    // for by name.
    // -----------------------------------------------------------------------

    #[test]
    fn provider_egress_hosts_maps_known_providers_and_rejects_unknown() {
        // The two runner-drivable providers map to their canonical API host(s).
        assert_eq!(
            provider_egress_hosts("anthropic").unwrap().to_vec(),
            vec!["api.anthropic.com"]
        );
        assert_eq!(
            provider_egress_hosts("openrouter").unwrap().to_vec(),
            vec!["openrouter.ai"]
        );

        // `openai` and `gemini` are not runner-drivable today, so they are NOT
        // known providers: they fall through to `None` rather than minting an
        // egress route to a host the harness cannot talk to (#362).
        assert!(provider_egress_hosts("openai").is_none());
        assert!(provider_egress_hosts("gemini").is_none());

        // Anything that is not a canonical provider name is unknown: a bare
        // domain, a host, the empty string.
        assert!(provider_egress_hosts("acme.com").is_none());
        assert!(provider_egress_hosts("api.anthropic.com").is_none());
        assert!(provider_egress_hosts("").is_none());

        // Case-sensitive: only the lowercase canonical names resolve, so an
        // uppercased spelling is rejected rather than silently normalized.
        assert!(provider_egress_hosts("Anthropic").is_none());
        assert!(provider_egress_hosts("ANTHROPIC").is_none());
    }

    #[test]
    fn parse_egress_provider_accepts_known_and_errs_usage_on_unknown() {
        // Each runner-drivable provider parses to its own canonical name.
        for p in ["anthropic", "openrouter"] {
            assert_eq!(parse_egress_provider(p).unwrap(), p);
        }

        // `openai` and `gemini` are no longer accepted -- the runner cannot
        // drive them, so they are usage errors like any other unknown value.
        for p in ["openai", "gemini"] {
            assert_eq!(
                parse_egress_provider(p).unwrap_err().class,
                crate::exit::ExitClass::Usage
            );
        }

        // An unknown value is a deterministic input error (exit 2 / Usage).
        let err = parse_egress_provider("acme.com").unwrap_err();
        assert_eq!(err.class, crate::exit::ExitClass::Usage);
        assert!(err.message.contains("acme.com"), "{}", err.message);
        assert!(
            err.message.contains("not a known provider"),
            "{}",
            err.message
        );
        // The message enumerates the accepted providers so the operator can fix
        // the flag without reading source.
        for p in ["anthropic", "openrouter"] {
            assert!(
                err.message.contains(p),
                "message should list `{p}`: {}",
                err.message
            );
        }
        // ...and does NOT advertise the providers the runner cannot drive.
        assert!(
            !err.message.contains("openai") && !err.message.contains("gemini"),
            "message should not list undrivable providers: {}",
            err.message
        );
        // The fix hint points at the escape hatch for arbitrary destinations.
        let fix = err.fix.expect("a usage error should carry a fix hint");
        assert!(fix.contains("--allow-web-egress"), "{fix}");

        // Case-sensitivity is enforced here too: `Anthropic` is not `anthropic`.
        assert_eq!(
            parse_egress_provider("Anthropic").unwrap_err().class,
            crate::exit::ExitClass::Usage
        );
    }

    #[test]
    fn ip_to_egress_cidr_appends_full_host_prefix() {
        use std::net::IpAddr;
        // An IPv4 host is a /32; an IPv6 host is a /128 -- a single-host CIDR so
        // the egress rule opens exactly that resolved address, nothing wider.
        let v4: IpAddr = "1.2.3.4".parse().unwrap();
        assert_eq!(ip_to_egress_cidr(v4), "1.2.3.4/32");
        let v6: IpAddr = "2001:db8::1".parse().unwrap();
        assert_eq!(ip_to_egress_cidr(v6), "2001:db8::1/128");
    }

    #[test]
    fn resolve_provider_egress_cidrs_dedups_sorts_and_covers_all_hosts() {
        use std::net::IpAddr;
        // Injected resolver so the test never touches real DNS. Anthropic and
        // OpenRouter share 1.1.1.1 to prove deduplication; Anthropic also
        // yields an IPv6 address to prove the v4/v6 mix. All addresses are
        // globally routable so they survive the split-horizon guard.
        let resolve = |host: &str| -> std::io::Result<Vec<IpAddr>> {
            Ok(match host {
                "api.anthropic.com" => {
                    vec![
                        "1.1.1.1".parse().unwrap(),
                        "2606:4700::1111".parse().unwrap(),
                    ]
                }
                "openrouter.ai" => {
                    vec!["1.1.1.1".parse().unwrap(), "1.0.0.1".parse().unwrap()]
                }
                other => panic!("unexpected host {other}"),
            })
        };
        let providers = vec!["anthropic".to_string(), "openrouter".to_string()];
        let cidrs = resolve_provider_egress_cidrs(&providers, resolve).unwrap();
        // Deduplicated (one 1.1.1.1/32) and sorted for a stable install argv.
        assert_eq!(
            cidrs,
            vec!["1.0.0.1/32", "1.1.1.1/32", "2606:4700::1111/128"]
        );
    }

    #[test]
    fn resolve_provider_egress_cidrs_errs_when_host_resolves_empty() {
        use std::net::IpAddr;
        // A host that resolves to nothing is a hard error naming the host, not a
        // silent skip -- a real model call would otherwise fail closed with no
        // clue why.
        let resolve = |_host: &str| -> std::io::Result<Vec<IpAddr>> { Ok(vec![]) };
        let err = resolve_provider_egress_cidrs(&["anthropic".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("api.anthropic.com"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_propagates_resolver_error_naming_host() {
        use std::net::IpAddr;
        // A resolver failure propagates as an error that names the host that
        // failed to resolve.
        let resolve = |host: &str| -> std::io::Result<Vec<IpAddr>> {
            Err(std::io::Error::other(format!("dns down for {host}")))
        };
        let err = resolve_provider_egress_cidrs(&["openrouter".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("openrouter.ai"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_errs_on_unknown_provider() {
        use std::net::IpAddr;
        // An unknown provider in the slice fails loudly (should be pre-validated,
        // but never silently skipped).
        let resolve =
            |_host: &str| -> std::io::Result<Vec<IpAddr>> { Ok(vec!["10.0.0.1".parse().unwrap()]) };
        let err = resolve_provider_egress_cidrs(&["acme.com".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("acme.com"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_rejects_imds_address() {
        use std::net::IpAddr;
        // A poisoned DNS answer mapping a provider host to the node metadata
        // endpoint must fail loud, naming both the host and the address.
        let resolve = |_host: &str| -> std::io::Result<Vec<IpAddr>> {
            Ok(vec!["169.254.169.254".parse().unwrap()])
        };
        let err = resolve_provider_egress_cidrs(&["anthropic".to_string()], resolve).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("api.anthropic.com"), "{msg}");
        assert!(msg.contains("169.254.169.254"), "{msg}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_rejects_private_v4() {
        use std::net::IpAddr;
        let resolve =
            |_host: &str| -> std::io::Result<Vec<IpAddr>> { Ok(vec!["10.0.0.5".parse().unwrap()]) };
        let err = resolve_provider_egress_cidrs(&["openrouter".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("10.0.0.5"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_rejects_non_routable_v6() {
        use std::net::IpAddr;
        // Loopback, link-local, and ULA v6 answers all fail closed.
        for addr in ["::1", "fe80::1", "fc00::1"] {
            let resolve = move |_host: &str| -> std::io::Result<Vec<IpAddr>> {
                Ok(vec![addr.parse().unwrap()])
            };
            let err =
                resolve_provider_egress_cidrs(&["openrouter".to_string()], resolve).unwrap_err();
            assert!(format!("{err:#}").contains(addr), "{addr}: {err:#}");
        }
    }

    #[test]
    fn resolve_provider_egress_cidrs_accepts_public_addresses() {
        use std::net::IpAddr;
        // A normal public v4 + v6 pair mints the expected single-host CIDRs.
        let resolve = |_host: &str| -> std::io::Result<Vec<IpAddr>> {
            Ok(vec![
                "1.1.1.1".parse().unwrap(),
                "2606:4700::1111".parse().unwrap(),
            ])
        };
        let cidrs = resolve_provider_egress_cidrs(&["anthropic".to_string()], resolve).unwrap();
        assert_eq!(cidrs, vec!["1.1.1.1/32", "2606:4700::1111/128"]);
    }

    #[test]
    fn resolve_provider_egress_cidrs_rejects_mix_with_one_private() {
        use std::net::IpAddr;
        // A host that resolves to a public AND a private address fails loud --
        // the private one must never be silently dropped.
        let resolve = |_host: &str| -> std::io::Result<Vec<IpAddr>> {
            Ok(vec![
                "1.1.1.1".parse().unwrap(),
                "10.0.0.5".parse().unwrap(),
            ])
        };
        let err = resolve_provider_egress_cidrs(&["anthropic".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("10.0.0.5"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_rejects_ipv4_mapped_private_v6() {
        use std::net::IpAddr;
        // An IPv4-mapped v6 of a private v4 is unmapped and re-checked, so it
        // is rejected just like the bare private v4.
        let resolve = |_host: &str| -> std::io::Result<Vec<IpAddr>> {
            Ok(vec!["::ffff:10.0.0.5".parse().unwrap()])
        };
        let err = resolve_provider_egress_cidrs(&["openrouter".to_string()], resolve).unwrap_err();
        assert!(format!("{err:#}").contains("10.0.0.5"), "{err:#}");
    }

    #[test]
    fn resolve_provider_egress_cidrs_routability_table() {
        use std::net::IpAddr;
        // Every non-globally-routable range must fail closed (Err), and every
        // public address must succeed (Ok). Injecting a single resolved answer
        // per case exercises `is_globally_routable_egress` end to end through
        // the resolver seam.
        let cases: &[(&str, bool)] = &[
            // Non-routable v4 -- each must be rejected.
            ("0.0.0.0", false),         // 0.0.0.0/8 / unspecified
            ("10.0.0.5", false),        // private 10/8
            ("100.64.0.1", false),      // CGNAT 100.64.0.0/10
            ("169.254.169.254", false), // link-local / IMDS
            ("192.0.0.1", false),       // IETF protocol assignments 192.0.0.0/24
            ("192.88.99.1", false),     // 6to4 relay anycast 192.88.99.0/24
            ("198.18.0.1", false),      // benchmarking 198.18.0.0/15
            ("240.0.0.1", false),       // reserved/future 240.0.0.0/4
            ("255.255.255.255", false), // broadcast (240/4)
            // Non-routable v6 -- each must be rejected.
            ("::1", false),             // loopback
            ("fe80::1", false),         // link-local
            ("fc00::1", false),         // ULA
            ("2001:db8::1", false),     // documentation
            ("::ffff:10.0.0.5", false), // IPv4-mapped private
            // Public addresses -- each must succeed.
            ("1.1.1.1", true),
            ("8.8.8.8", true),
            ("2606:4700::1111", true),
            ("2001:4860:4860::8888", true),
        ];
        for (addr, expect_ok) in cases {
            let a = *addr;
            let resolve =
                move |_host: &str| -> std::io::Result<Vec<IpAddr>> { Ok(vec![a.parse().unwrap()]) };
            let res = resolve_provider_egress_cidrs(&["anthropic".to_string()], resolve);
            if *expect_ok {
                let cidrs = res.unwrap_or_else(|e| panic!("{a} should be routable: {e:#}"));
                assert_eq!(cidrs.len(), 1, "{a} should mint one CIDR");
            } else {
                let err = res
                    .err()
                    .unwrap_or_else(|| panic!("{a} should be rejected as non-routable"));
                assert!(format!("{err:#}").contains(a), "{a}: {err:#}");
            }
        }
    }

    #[test]
    fn provider_egress_note_none_on_empty_and_lists_providers() {
        // No providers -> no note.
        assert!(provider_egress_note(&[]).is_none());
        // Non-empty -> a note that says egress was opened and names each provider.
        let note = provider_egress_note(&["anthropic".to_string(), "openrouter".to_string()])
            .expect("a note for a non-empty provider list");
        assert!(note.contains("egress opened"), "{note}");
        assert!(note.contains("anthropic"), "{note}");
        assert!(note.contains("openrouter"), "{note}");
    }

    #[test]
    fn sealed_credential_warning_only_when_cred_present_and_no_egress() {
        // The one combination that warns: a credential is present but nothing
        // opened egress, so the model is unreachable behind the sealed sandbox.
        let warn =
            sealed_credential_warning(true, false).expect("cred present + no egress must warn");
        assert!(warn.contains("sealed"), "{warn}");
        assert!(warn.contains("unreachable"), "{warn}");
        assert!(warn.contains("--allow-egress-host"), "{warn}");
        assert!(warn.contains("--allow-web-egress"), "{warn}");

        // Every other combination stays silent.
        assert!(sealed_credential_warning(true, true).is_none());
        assert!(sealed_credential_warning(false, false).is_none());
        assert!(sealed_credential_warning(false, true).is_none());
    }

    #[test]
    fn model_egress_status_lines_no_cred_open_egress_never_says_sealed() {
        // The exact contradiction bug: no credential but egress opened via a
        // provider. The provider note must report the open, and the fake-model
        // warning must NOT claim the egress is sealed.
        let lines =
            model_egress_status_lines(false, false, false, &["anthropic".to_string()], true, false);
        let msgs: Vec<&str> = lines.iter().map(|(_, m)| m.as_str()).collect();
        assert!(msgs.iter().any(|m| m.contains("egress opened")), "{msgs:?}");
        for m in &msgs {
            assert!(!m.contains("sealed"), "{m}");
        }
    }

    #[test]
    fn model_egress_status_lines_cred_no_egress_warns_sealed() {
        // A credential present with nothing opened surfaces the sealed warning
        // naming both flags.
        let lines = model_egress_status_lines(true, false, false, &[], false, false);
        let warn = lines
            .iter()
            .find(|(w, _)| *w)
            .map(|(_, m)| m.as_str())
            .expect("a warn line");
        assert!(warn.contains("sealed"), "{warn}");
        assert!(warn.contains("--allow-egress-host"), "{warn}");
        assert!(warn.contains("--allow-web-egress"), "{warn}");
    }

    #[test]
    fn model_egress_status_lines_cred_open_egress_no_sealed() {
        // A credential with a provider egress opened: provider note + rotation
        // present, and no message claims the sandbox is sealed.
        let lines =
            model_egress_status_lines(true, false, false, &["openrouter".to_string()], true, false);
        let msgs: Vec<&str> = lines.iter().map(|(_, m)| m.as_str()).collect();
        assert!(msgs.iter().any(|m| m.contains("egress opened")), "{msgs:?}");
        assert!(msgs.iter().any(|m| m.contains("can rotate")), "{msgs:?}");
        for m in &msgs {
            assert!(!m.contains("sealed"), "{m}");
        }
    }

    #[test]
    fn model_egress_status_lines_fake_model_sealed_and_canned() {
        // No credential, no egress, real (not --fake-model) install: the
        // fake-model warning keeps the "(model egress stays sealed)" clause and
        // a canned-replies note follows.
        let lines = model_egress_status_lines(false, false, false, &[], false, false);
        let msgs: Vec<&str> = lines.iter().map(|(_, m)| m.as_str()).collect();
        assert!(
            msgs.iter()
                .any(|m| m.contains("(model egress stays sealed)")),
            "{msgs:?}"
        );
        assert!(
            msgs.iter().any(|m| m.contains("Replies will be canned")),
            "{msgs:?}"
        );
    }

    #[test]
    fn model_egress_status_lines_dry_run_skips_past_tense_note() {
        // Under dry-run the handler prints its own "a live run resolves..."
        // note, so this fn emits no past-tense "egress opened" line.
        let lines =
            model_egress_status_lines(true, false, false, &["anthropic".to_string()], true, true);
        for (_, m) in &lines {
            assert!(!m.contains("egress opened"), "{m}");
        }
    }

    #[test]
    fn up_emits_resolved_provider_cidrs_before_web_egress_contiguously() {
        // Resolved provider CIDRs take the first slots (in order), then declared
        // web destinations continue contiguously -- one array, no gaps.
        let cmds = up_commands(&UpOpts {
            common: common(),
            model: None,
            allow_egress_host: vec!["anthropic".into()],
            resolved_egress_cidrs: vec!["10.0.0.1/32".into(), "2001:db8::1/128".into()],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec!["203.0.113.0/24".into()],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
        });
        let line = cmds[0].display();
        // Provider CIDRs occupy [0] and [1], each with the shared TCP/443 shape.
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=10.0.0.1/32'"),
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
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[1].cidr=2001:db8::1/128'"),
            "{line}"
        );
        // The declared web destination continues at the next index, not [0].
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[2].cidr=203.0.113.0/24'"),
            "{line}"
        );
        // The old unconditional Anthropic carve-out is gone.
        assert!(!line.contains("160.79.104.0/23"), "{line}");
    }

    #[test]
    fn up_credential_without_any_egress_emits_no_allowed_egress() {
        // A credential with neither a resolved provider CIDR nor a web egress
        // destination enables the real model but opens NO egress -- the old
        // unconditional Anthropic carve-out is removed entirely (#362). The
        // sandbox stays sealed and the model is unreachable by design.
        let cmds = up_commands(&UpOpts {
            common: common(),
            model: None,
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec![],
            fake_model: false,
            credentials: Some("sk-ant-secretsecret".into()),
            local_model: None,
        });
        let line = cmds[0].display();
        // Real model still enabled and the credential still delivered by file.
        assert!(
            line.contains("agentSandbox.runner.fakeModel=false"),
            "{line}"
        );
        assert!(line.contains("-f '<secret values file:"), "{line}");
        // But NO egress rule at all -- and specifically not the old Anthropic one.
        assert!(!line.contains("160.79.104.0/23"), "{line}");
        assert!(!line.contains("allowedEgress"), "{line}");
    }

    #[test]
    fn up_web_egress_alone_still_starts_at_index_zero() {
        // Existing behavior preserved: with no credential and no provider host,
        // a declared web destination still occupies index [0].
        let cmds = up_commands(&UpOpts {
            common: common(),
            model: None,
            allow_egress_host: vec![],
            resolved_egress_cidrs: vec![],
            chart: "charts/agentos".into(),
            secrets: vec![],
            dev: false,
            no_expose: true,
            set: vec![],
            allow_web_egress: vec!["203.0.113.0/24".into()],
            fake_model: true,
            credentials: None,
            local_model: None,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("'security.networkPolicy.allowedEgress[0].cidr=203.0.113.0/24'"),
            "{line}"
        );
        assert!(!line.contains("allowedEgress[1]"), "{line}");
        assert!(!line.contains("160.79.104.0/23"), "{line}");
    }
}
