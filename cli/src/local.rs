//! `agentos local <up|down|status>`: wrap the repo's local dev stack
//! (`compose.dev.yaml`: Postgres + Valkey + Langfuse + ClickHouse + MinIO +
//! OTel) the same way `ops.rs` wraps Helm -- a deliberately thin CLI over
//! `docker compose`, which stays the source of truth. Each verb builds its
//! command line as a pure function returning an [`OpsCommand`]; the executor
//! (or the `--dry-run` printer) consumes it, so argv construction stays
//! unit-testable with no Docker daemon.

use anyhow::{bail, Context, Result};

use crate::commands::OLLAMA_PORT;
use crate::docker;
use crate::ops::{plain, require_on_path, run_capture, run_step, OpsCommand};

/// Dev-channel local-candidate filename probed by the artifact resolver.
pub const DEFAULT_COMPOSE_FILE: &str = "compose.dev.yaml";

/// The service endpoints the dev stack exposes, as committed in
/// `compose.dev.yaml`'s port mappings. Printed after `local up` so the operator
/// has the URLs in hand. Hardcoded to match the compose file (see the
/// `endpoints_match_compose_file` test, which asserts the file still maps them).
///
/// The `core` flag marks endpoints backed by a service in the `core` profile
/// (started under `--minimal`); the rest are `full`-only and are hidden under
/// `--minimal` so `up` never advertises a URL for a service it did not start.
const ENDPOINTS: &[(&str, &str, bool)] = &[
    // The three observability port literals live once, in `observability.rs`
    // (#460); these rows reference them so the two cannot drift. Values are
    // unchanged, so `endpoints_match_compose_file` stays green.
    ("AgentOS API", crate::observability::LOCAL_API_URL, true),
    (
        "AgentOS Console",
        crate::observability::LOCAL_CONSOLE_URL,
        false,
    ),
    (
        "Langfuse UI",
        crate::observability::LOCAL_LANGFUSE_URL,
        false,
    ),
    ("Postgres", "localhost:25432", true),
    ("Valkey", "localhost:26379", true),
    ("ClickHouse HTTP", "localhost:28123", false),
    ("MinIO S3", "localhost:29000", true),
    ("MinIO console", "localhost:29001", true),
    ("OTel gRPC", "localhost:24317", false),
    ("OTel HTTP", "localhost:24318", false),
];

/// Credential env vars the compose stack forwards from the shell (bare names in
/// `compose.dev.yaml`). Any one set non-empty makes `local up` go live, matching
/// `skill up`. Empty counts as unset (the empty-string-is-not-a-credential rule).
pub const CREDENTIAL_ENV_VARS: &[&str] = &[
    "AGENTOS_CREDENTIALS",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
];

/// The model mode `local up` resolves from the shell so the local tier reaches
/// skill-tier parity: a credential present makes local go live exactly like
/// `skill up`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ModelMode {
    /// A credential is present and fake is not pinned truthy: inject
    /// AGENTOS_FAKE_MODEL=0 so local goes live like `skill up`.
    LiveFromCredential,
    /// A credential is present but AGENTOS_FAKE_MODEL is pinned truthy: run fake
    /// anyway (the operator asked for it) but warn loudly.
    FakePinnedDespiteCredential,
    /// No credential: compose's fake default stands; nothing to inject.
    DefaultFake,
}

/// Match the runner's truthy parse of `AGENTOS_FAKE_MODEL`
/// (`runner/src/agentos_runner/__main__.py`): lowercase one of `1`/`true`/`yes`.
fn fake_model_is_truthy(v: &str) -> bool {
    matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes")
}

/// Pure parity core. `explicit_fake_model` is the shell AGENTOS_FAKE_MODEL (None
/// when unset or empty). `has_credential` is whether any CREDENTIAL_ENV_VARS is
/// set non-empty.
pub fn resolve_model_mode(explicit_fake_model: Option<&str>, has_credential: bool) -> ModelMode {
    if !has_credential {
        return ModelMode::DefaultFake;
    }
    match explicit_fake_model {
        Some(v) if fake_model_is_truthy(v) => ModelMode::FakePinnedDespiteCredential,
        _ => ModelMode::LiveFromCredential,
    }
}

/// The single injection step every worker-restarting command shares: flip
/// compose's fake default to live when (and only when) a credential is present
/// and fake is not explicitly pinned. `FakePinnedDespiteCredential` and
/// `DefaultFake` return `None` so compose's `${AGENTOS_FAKE_MODEL:-1}` default
/// stands. This is the one place that decision is made -- `up_command` and
/// `local comms`'s connect/disconnect commands all call it instead of each
/// re-deriving the pair inline, which is what let `local comms` drift out of
/// parity with `local up` (issue #450).
pub fn fake_model_env_override(mode: ModelMode) -> Option<(String, String)> {
    match mode {
        ModelMode::LiveFromCredential => Some(("AGENTOS_FAKE_MODEL".into(), "0".into())),
        ModelMode::FakePinnedDespiteCredential | ModelMode::DefaultFake => None,
    }
}

/// The other injection step every worker-restarting command shares: suppress
/// the OTel endpoint on a `core`-only stack. `otel-collector` is a `full`-profile
/// service, so a `--minimal` stack has no collector to export to, and every span
/// the runner emits would pay a synchronous DNS retry against a name that cannot
/// resolve. An empty value (not an absent one) is what does it: compose writes
/// the endpoint as `${OTEL_EXPORTER_OTLP_ENDPOINT-...}`, whose `-` (unset-only)
/// form substitutes its default only when the var is UNSET, so exporting it
/// empty resolves to empty and the runner exports nothing. `false` returns
/// `None` so compose's shipped collector default stands. This is the one place
/// that decision is made -- `up_command` and `local comms`'s connect/disconnect
/// commands all call it instead of each re-deriving the pair inline, the same
/// drift that let `local comms` fall out of parity with `local up` on the fake
/// model (issue #450).
pub fn otel_endpoint_env_override(minimal: bool) -> Option<(String, String)> {
    if minimal {
        Some(("OTEL_EXPORTER_OTLP_ENDPOINT".into(), String::new()))
    } else {
        None
    }
}

/// Snapshot the shell for the parity decision. An empty AGENTOS_FAKE_MODEL is
/// treated as unset (matches compose's `${AGENTOS_FAKE_MODEL:-1}` and the
/// empty-string-is-not-a-credential rule); a credential is any non-empty
/// CREDENTIAL_ENV_VARS value.
pub fn model_mode_from_env() -> ModelMode {
    let explicit = std::env::var("AGENTOS_FAKE_MODEL")
        .ok()
        .filter(|s| !s.is_empty());
    let has_credential = CREDENTIAL_ENV_VARS
        .iter()
        .any(|v| std::env::var(v).map(|s| !s.is_empty()).unwrap_or(false));
    resolve_model_mode(explicit.as_deref(), has_credential)
}

/// Flags shared by every `local` verb.
pub struct LocalOpts {
    pub file: String,
    pub dry_run: bool,
    pub minimal: bool,
    pub local_model: Option<String>,
    pub slack: bool,
    /// Model mode resolved from the shell (skill-tier parity). Only `up`
    /// consumes it; `down`/`status` set `DefaultFake`.
    pub model_mode: ModelMode,
}

pub struct LocalDownOpts {
    pub common: LocalOpts,
    /// Add `-v` to destroy volumes (throwaway).
    pub wipe: bool,
    /// Skip the interactive confirmation that `--wipe` otherwise requires.
    pub yes: bool,
}

// ---------------------------------------------------------------------------
// Command builders (pure; unit-tested below)
// ---------------------------------------------------------------------------

/// `docker compose -f <file> <tail...>`.
fn compose(file: &str, tail: &[&str]) -> OpsCommand {
    let mut args = vec![plain("compose"), plain("-f"), plain(file)];
    for t in tail {
        args.push(plain(*t));
    }
    OpsCommand::new("docker", args)
}

/// `docker compose --profile <core|full> [--profile local-model] [--profile slack] -f <file> up -d --wait`.
pub fn up_command(o: &LocalOpts) -> OpsCommand {
    let profile = if o.minimal { "core" } else { "full" };
    let mut args = vec![plain("compose"), plain("--profile"), plain(profile)];
    if o.local_model.is_some() {
        args.push(plain("--profile"));
        args.push(plain("local-model"));
    }
    if o.slack {
        args.push(plain("--profile"));
        args.push(plain("slack"));
    }
    args.extend([
        plain("-f"),
        plain(&o.file),
        plain("up"),
        plain("-d"),
        plain("--wait"),
    ]);
    let mut cmd = OpsCommand::new("docker", args);
    // `with_env` REPLACES the env vec, so build it once. `--local-model` and the
    // credential-driven live injection are mutually exclusive: local-model
    // carries its own live env (AGENTOS_FAKE_MODEL=0 + the ollama routing), so
    // the parity injection only applies when no local model is requested.
    let mut env: Vec<(String, String)> = if let Some(model) = &o.local_model {
        vec![
            ("AGENTOS_FAKE_MODEL".into(), "0".into()),
            (
                "AGENTOS_MODEL_BASE_URL".into(),
                format!("http://ollama:{OLLAMA_PORT}"),
            ),
            ("AGENTOS_MODEL".into(), model.clone()),
            ("AGENTOS_DOCKER_NETWORK".into(), "agentos_default".into()),
            // Pin the compose project name so the default network is always
            // `agentos_default`, regardless of the working-directory basename
            // (which is what compose otherwise derives the project name from).
            ("COMPOSE_PROJECT_NAME".into(), "agentos".into()),
        ]
    } else {
        // Delegate to `fake_model_env_override`, which discriminates on
        // `o.model_mode`: LiveFromCredential injects AGENTOS_FAKE_MODEL=0 so
        // compose goes live, matching `skill up`. FakePinnedDespiteCredential
        // and DefaultFake inject nothing, so compose's
        // `${AGENTOS_FAKE_MODEL:-1}` default stands for those two modes.
        fake_model_env_override(o.model_mode).into_iter().collect()
    };
    // Delegate to `otel_endpoint_env_override`, the single source of truth for
    // the `core`-profile collector suppression. This sits AFTER the branch
    // above, not inside it, because the `--local-model` arm does not fall
    // through to the else: `--minimal --local-model` needs suppressing too.
    env.extend(otel_endpoint_env_override(o.minimal));
    if !env.is_empty() {
        cmd = cmd.with_env(env);
    }
    cmd
}

/// Every compose profile `up` can activate. `down` passes all of them
/// unconditionally so it always tears down what any `up` could start -- most
/// importantly the `slack` dispatcher (`restart: unless-stopped`), which a bare
/// `down` leaves running after a forgot-to-disconnect where it keeps holding the
/// Socket Mode connection and posting placeholders into real Slack with no
/// backend (issue #233). These are deliberately independent of the `LocalOpts`
/// flags: a plain `local down` carries none of `--slack`/`--minimal`/
/// `--local-model`, so gating teardown on them would orphan exactly the
/// profile-only services this exists to reap.
const ALL_PROFILES: &[&str] = &["core", "full", "local-model", "slack"];

/// `docker compose --profile <all> -f <file> down` (keep volumes), or
/// `... down -v` with `--wipe` (destroy volumes). The profiles cover every
/// service `up` can start so `down` never orphans a profile-only container.
pub fn down_command(o: &LocalDownOpts) -> OpsCommand {
    let mut args = vec![plain("compose")];
    for p in ALL_PROFILES {
        args.push(plain("--profile"));
        args.push(plain(*p));
    }
    args.extend([plain("-f"), plain(&o.common.file), plain("down")]);
    if o.wipe {
        args.push(plain("-v"));
    }
    OpsCommand::new("docker", args)
}

/// `docker compose -f <file> ps`.
pub fn status_command(o: &LocalOpts) -> OpsCommand {
    compose(&o.file, &["ps"])
}

// ---------------------------------------------------------------------------
// Verb handlers
// ---------------------------------------------------------------------------

pub async fn up(o: LocalOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = up_command(&o);
    if o.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: vec![cmd.display()],
        });
        return Ok(());
    }
    require_on_path("docker")?;
    let cl = ui.checklist();
    run_step(&cl, "starting dev stack", "up", &cmd).await?;
    // `--local-model` is its own live path (routes to ollama); the shell-credential
    // parity note only applies when no local model was requested.
    if o.local_model.is_none() {
        match o.model_mode {
            ModelMode::LiveFromCredential => ui.note(
                "Running the LIVE model: a credential is set in your shell (parity with `agentos skill up`). Set AGENTOS_FAKE_MODEL=1 to force the offline fake model.",
            ),
            ModelMode::FakePinnedDespiteCredential => ui.warn(
                "Running the FAKE model despite a credential in your shell: AGENTOS_FAKE_MODEL is pinned on. Unset it or set AGENTOS_FAKE_MODEL=0 to go live.",
            ),
            ModelMode::DefaultFake => ui.note(
                "Running the fake model (no credential set). Provide a credential (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / AGENTOS_CREDENTIALS) or --local-model to go live.",
            ),
        }
    }
    for (label, url, is_core) in ENDPOINTS {
        // Under `--minimal` only the `core` services started, so advertise only
        // their endpoints; the `full`-only URLs would 404.
        if !o.minimal || *is_core {
            ui.kv(label, &ui.url(url));
        }
    }
    if o.slack {
        ui.note("Slack dispatcher started (Socket Mode; no host port).");
    }
    ui.note("Drive the local product loop (no Slack, no Kubernetes):");
    ui.note(
        "  agentos local deploy --plugin-dir <dir> --slack-channel <C...> --api-url http://localhost:28000",
    );
    ui.note("  agentos local message \"<your question>\"");
    Ok(())
}

pub async fn status(o: LocalOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = status_command(&o);
    if o.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: vec![cmd.display()],
        });
        return Ok(());
    }
    require_on_path("docker")?;
    // `docker compose ps` output is itself the payload table.
    let (ok, out, err) = run_capture(&cmd).await?;
    if !ok {
        for line in err.lines() {
            ui.plumbing(line);
        }
        let reason = err
            .lines()
            .rev()
            .map(str::trim)
            .find(|l| !l.is_empty())
            .unwrap_or("command failed");
        ui.failure(&format!("`docker compose ps` failed: {reason}"));
        bail!("`docker compose ps` exited nonzero");
    }
    for line in out.lines() {
        ui.payload_plain(line);
    }
    Ok(())
}

pub async fn down(o: LocalDownOpts) -> Result<()> {
    let ui = crate::ui::ui();
    let cmd = down_command(&o);
    if o.common.dry_run {
        ui.emit(&crate::ui::DryRunPlan {
            lines: vec![
                cmd.display(),
                format!(
                    "docker rm -f $(docker ps -a --filter label={} -q)",
                    docker::SANDBOX_LABEL
                ),
            ],
        });
        return Ok(());
    }
    if o.wipe {
        ui.warn(&format!(
            "this destroys all volumes for the '{}' dev stack (Postgres, ClickHouse, MinIO, Valkey data)",
            o.common.file
        ));
        if !o.yes && !confirm_wipe(&o.common.file)? {
            ui.note("aborted");
            return Ok(());
        }
    }
    require_on_path("docker")?;
    let cl = ui.checklist();
    let label = if o.wipe {
        "stopping stack and wiping volumes"
    } else {
        "stopping stack"
    };
    run_step(&cl, label, "stopped", &cmd).await?;
    let count = docker::reap_labeled(docker::SANDBOX_LABEL).await?;
    if count > 0 {
        ui.note(&format!("removed {count} runner container(s)"));
    }
    if o.wipe {
        ui.payload("dev stack stopped; volumes wiped");
    } else {
        ui.payload("dev stack stopped");
        ui.note("volumes kept (fast restart with `agentos local up`)");
    }
    Ok(())
}

/// Read a y/N confirmation from stderr/stdin before `--wipe` destroys volumes.
fn confirm_wipe(file: &str) -> Result<bool> {
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
        "This destroys all volumes for the '{file}' dev stack (Postgres, ClickHouse, MinIO, Valkey data). Continue? [y/N] "
    );
    std::io::stderr().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .context("reading confirmation from stdin")?;
    Ok(matches!(line.trim(), "y" | "Y" | "yes" | "Yes"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(file: &str) -> LocalOpts {
        LocalOpts {
            file: file.into(),
            dry_run: false,
            minimal: false,
            local_model: None,
            slack: false,
            model_mode: ModelMode::DefaultFake,
        }
    }

    fn opts_with_local_model(file: &str, model: &str) -> LocalOpts {
        LocalOpts {
            file: file.into(),
            dry_run: false,
            minimal: false,
            local_model: Some(model.into()),
            slack: false,
            model_mode: ModelMode::DefaultFake,
        }
    }

    /// Every `--profile` token `up` can emit across all flag combinations,
    /// derived from `up_command` itself so a newly added up profile that `down`
    /// forgets fails `down_passes_every_up_profile` instead of silently
    /// orphaning that service. `--minimal` swaps `full`->`core`, so both modes
    /// are sampled with `--slack` and `--local-model` on.
    fn up_activatable_profiles() -> std::collections::BTreeSet<String> {
        let mut profiles = std::collections::BTreeSet::new();
        for minimal in [false, true] {
            let mut o = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
            o.minimal = minimal;
            o.slack = true;
            let display = up_command(&o).display();
            let mut tokens = display.split_whitespace().peekable();
            while let Some(tok) = tokens.next() {
                if tok == "--profile" {
                    if let Some(p) = tokens.next() {
                        profiles.insert(p.to_string());
                    }
                }
            }
        }
        profiles
    }

    fn read_compose(name: &str) -> String {
        std::fs::read_to_string(format!("{}/../{}", env!("CARGO_MANIFEST_DIR"), name))
            .unwrap_or_else(|e| panic!("read {name}: {e}"))
    }

    #[test]
    fn up_uses_detached_wait() {
        let cmd = up_command(&opts(DEFAULT_COMPOSE_FILE));
        assert_eq!(
            cmd.display(),
            "docker compose --profile full -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn up_local_model_uses_profile_and_env() {
        let cmd = up_command(&opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b"));
        let display = cmd.display();
        assert!(display.contains("--profile full"), "{display}");
        assert!(display.contains("--profile local-model"), "{display}");
        assert!(display.contains("up -d --wait"), "{display}");
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_FAKE_MODEL"), String::from("0"))));
        assert!(cmd.env.contains(&(
            String::from("AGENTOS_MODEL_BASE_URL"),
            String::from("http://ollama:11434"),
        )));
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
        assert!(cmd.env.contains(&(
            String::from("AGENTOS_DOCKER_NETWORK"),
            String::from("agentos_default"),
        )));
        assert!(cmd.env.contains(&(
            String::from("COMPOSE_PROJECT_NAME"),
            String::from("agentos"),
        )));
    }

    /// `--minimal` starts the `core` profile, which has no otel-collector, so
    /// `up` must hand compose an EMPTY endpoint. Compose's `${VAR-default}` form
    /// substitutes only when the var is unset, so the empty value suppresses the
    /// default instead of pointing every spawned runner at a host that never
    /// resolves (each span then eats ~7s of synchronous export retry).
    #[test]
    fn up_minimal_suppresses_otel_endpoint() {
        let mut o = opts(DEFAULT_COMPOSE_FILE);
        o.minimal = true;
        let cmd = up_command(&o);
        assert!(
            cmd.env
                .contains(&(String::from("OTEL_EXPORTER_OTLP_ENDPOINT"), String::new(),)),
            "--minimal must pass an empty OTEL_EXPORTER_OTLP_ENDPOINT; env={:?}",
            cmd.env
        );
    }

    /// The `--local-model` arm of `up_command`'s env build does not fall through
    /// to the else, so the suppression has to sit outside both arms. This is the
    /// combination that regresses if it ever moves back inside one.
    #[test]
    fn up_minimal_suppresses_otel_endpoint_with_local_model() {
        let mut o = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        o.minimal = true;
        let cmd = up_command(&o);
        assert!(
            cmd.env
                .contains(&(String::from("OTEL_EXPORTER_OTLP_ENDPOINT"), String::new(),)),
            "--minimal --local-model must pass an empty OTEL_EXPORTER_OTLP_ENDPOINT; env={:?}",
            cmd.env
        );
        // The local-model wiring must survive the suppression.
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
    }

    /// The default (full-profile) `up` starts otel-collector, so it must NOT
    /// suppress: leaving the var unset is what lets compose's default resolve to
    /// `http://otel-collector:4318`.
    #[test]
    fn up_default_does_not_suppress_otel_endpoint() {
        for o in [
            opts(DEFAULT_COMPOSE_FILE),
            opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b"),
        ] {
            let cmd = up_command(&o);
            assert!(
                !cmd.env
                    .iter()
                    .any(|(k, _)| k == "OTEL_EXPORTER_OTLP_ENDPOINT"),
                "a non-minimal up must leave compose's endpoint default alone; env={:?}",
                cmd.env
            );
        }
    }

    #[test]
    fn resolve_model_mode_truth_table() {
        // No credential -> DefaultFake regardless of any pin.
        assert_eq!(resolve_model_mode(None, false), ModelMode::DefaultFake);
        assert_eq!(resolve_model_mode(Some("1"), false), ModelMode::DefaultFake);
        assert_eq!(
            resolve_model_mode(Some("banana"), false),
            ModelMode::DefaultFake
        );
        // Credential + no explicit pin -> live.
        assert_eq!(
            resolve_model_mode(None, true),
            ModelMode::LiveFromCredential
        );
        // Credential + truthy pin (any casing the runner accepts) -> fake pinned.
        for pin in ["1", "true", "YES", "Yes"] {
            assert_eq!(
                resolve_model_mode(Some(pin), true),
                ModelMode::FakePinnedDespiteCredential,
                "pin {pin:?} should pin fake"
            );
        }
        // Credential + non-truthy pin -> live (0/off/garbage are not "fake on").
        // A whitespace-padded value like " true " is not truthy because the
        // runner does not trim before comparing.
        for pin in ["0", "banana", "off", "", " true "] {
            assert_eq!(
                resolve_model_mode(Some(pin), true),
                ModelMode::LiveFromCredential,
                "pin {pin:?} should stay live"
            );
        }
    }

    #[test]
    fn fake_model_env_override_maps_all_three_modes() {
        assert_eq!(
            fake_model_env_override(ModelMode::LiveFromCredential),
            Some(("AGENTOS_FAKE_MODEL".to_string(), "0".to_string()))
        );
        assert_eq!(
            fake_model_env_override(ModelMode::FakePinnedDespiteCredential),
            None
        );
        assert_eq!(fake_model_env_override(ModelMode::DefaultFake), None);
    }

    #[test]
    fn up_live_from_credential_injects_fake_zero() {
        let mut o = opts(DEFAULT_COMPOSE_FILE);
        o.model_mode = ModelMode::LiveFromCredential;
        let cmd = up_command(&o);
        assert!(
            cmd.env
                .contains(&(String::from("AGENTOS_FAKE_MODEL"), String::from("0"))),
            "live-from-credential must inject AGENTOS_FAKE_MODEL=0; env={:?}",
            cmd.env
        );
        assert!(
            cmd.display().contains("AGENTOS_FAKE_MODEL=0"),
            "display must show the injected env: {}",
            cmd.display()
        );
    }

    #[test]
    fn up_fake_pinned_does_not_inject() {
        let mut o = opts(DEFAULT_COMPOSE_FILE);
        o.model_mode = ModelMode::FakePinnedDespiteCredential;
        let cmd = up_command(&o);
        assert!(
            !cmd.env.iter().any(|(k, _)| k == "AGENTOS_FAKE_MODEL"),
            "fake-pinned must leave compose's default alone; env={:?}",
            cmd.env
        );
    }

    #[test]
    fn up_default_fake_does_not_inject() {
        let cmd = up_command(&opts(DEFAULT_COMPOSE_FILE));
        assert!(
            !cmd.env.iter().any(|(k, _)| k == "AGENTOS_FAKE_MODEL"),
            "default-fake must leave compose's default alone; env={:?}",
            cmd.env
        );
    }

    #[test]
    fn up_local_model_unchanged_by_model_mode() {
        // --local-model owns the live env; a LiveFromCredential model_mode must
        // not duplicate or override it (exactly one AGENTOS_FAKE_MODEL=0, plus the
        // ollama routing env).
        let mut o = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        o.model_mode = ModelMode::LiveFromCredential;
        let cmd = up_command(&o);
        assert_eq!(
            cmd.env
                .iter()
                .filter(|(k, _)| k == "AGENTOS_FAKE_MODEL")
                .count(),
            1,
            "exactly one AGENTOS_FAKE_MODEL under --local-model; env={:?}",
            cmd.env
        );
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
        assert!(cmd.env.contains(&(
            String::from("AGENTOS_MODEL_BASE_URL"),
            String::from("http://ollama:11434"),
        )));
    }

    #[test]
    fn up_slack_appends_slack_profile() {
        let mut opts = opts(DEFAULT_COMPOSE_FILE);
        opts.slack = true;
        let cmd = up_command(&opts);
        assert_eq!(
            cmd.display(),
            "docker compose --profile full --profile slack -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn up_minimal_slack_uses_core_and_slack() {
        let mut opts = opts(DEFAULT_COMPOSE_FILE);
        opts.minimal = true;
        opts.slack = true;
        let display = up_command(&opts).display();
        assert!(display.contains("--profile core"), "{display}");
        assert!(display.contains("--profile slack"), "{display}");
        assert!(!display.contains("--profile full"), "{display}");
    }

    #[test]
    fn up_local_model_and_slack_keep_profile_order() {
        let mut opts = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        opts.slack = true;
        let display = up_command(&opts).display();
        assert!(
            display.contains("--profile full --profile local-model --profile slack"),
            "{display}"
        );
    }

    #[test]
    fn up_minimal_uses_core_profile() {
        let mut o = opts(DEFAULT_COMPOSE_FILE);
        o.minimal = true;
        let cmd = up_command(&o);
        // The empty endpoint is `--minimal`'s collector suppression (the `core`
        // profile starts no collector); `display` renders env before the program.
        assert_eq!(
            cmd.display(),
            "OTEL_EXPORTER_OTLP_ENDPOINT= docker compose --profile core -f compose.dev.yaml up -d --wait"
        );
    }

    #[test]
    fn minimal_and_local_model_combine() {
        let mut o = opts_with_local_model(DEFAULT_COMPOSE_FILE, "qwen3:4b");
        o.minimal = true;
        let cmd = up_command(&o);
        let display = cmd.display();
        assert!(display.contains("--profile core"), "{display}");
        assert!(display.contains("--profile local-model"), "{display}");
        assert!(!display.contains("--profile full"), "{display}");
        assert!(cmd
            .env
            .contains(&(String::from("AGENTOS_MODEL"), String::from("qwen3:4b"))));
        assert!(cmd.env.contains(&(
            String::from("COMPOSE_PROJECT_NAME"),
            String::from("agentos"),
        )));
    }

    #[test]
    fn status_runs_ps() {
        let cmd = status_command(&opts(DEFAULT_COMPOSE_FILE));
        assert_eq!(cmd.display(), "docker compose -f compose.dev.yaml ps");
    }

    #[test]
    fn down_keeps_volumes_by_default() {
        let cmd = down_command(&LocalDownOpts {
            common: opts(DEFAULT_COMPOSE_FILE),
            wipe: false,
            yes: false,
        });
        assert_eq!(
            cmd.display(),
            "docker compose --profile core --profile full --profile local-model --profile slack -f compose.dev.yaml down"
        );
    }

    #[test]
    fn down_wipe_adds_volume_flag() {
        let cmd = down_command(&LocalDownOpts {
            common: opts(DEFAULT_COMPOSE_FILE),
            wipe: true,
            yes: false,
        });
        assert_eq!(
            cmd.display(),
            "docker compose --profile core --profile full --profile local-model --profile slack -f compose.dev.yaml down -v"
        );
    }

    /// `down` must tear down every profile `up` can start, regardless of which
    /// flags this particular invocation carries. Concretely: a plain `local
    /// down` (no `--slack`) must still pass `--profile slack` so a
    /// forgot-to-disconnect dispatcher (`restart: unless-stopped`) is reaped
    /// instead of orphaned holding a live Socket Mode connection (issue #233).
    #[test]
    fn down_passes_every_up_profile() {
        // A default `local down` -- no --slack, no --minimal, no --local-model.
        let display = down_command(&LocalDownOpts {
            common: opts(DEFAULT_COMPOSE_FILE),
            wipe: false,
            yes: false,
        })
        .display();
        for profile in ["core", "full", "local-model", "slack"] {
            assert!(
                display.contains(&format!("--profile {profile}")),
                "down must pass --profile {profile}; got: {display}"
            );
        }
        // Every profile `up` can activate must be covered by `down`.
        for profile in up_activatable_profiles() {
            assert!(
                display.contains(&format!("--profile {profile}")),
                "down omits --profile {profile} that up can start; got: {display}"
            );
        }
    }

    #[test]
    fn custom_file_flows_through_every_verb() {
        let f = "compose.other.yaml";
        assert!(up_command(&opts(f))
            .display()
            .contains("-f compose.other.yaml"));
        assert!(status_command(&opts(f))
            .display()
            .contains("-f compose.other.yaml"));
        let down = down_command(&LocalDownOpts {
            common: opts(f),
            wipe: true,
            yes: true,
        });
        assert_eq!(
            down.display(),
            "docker compose --profile core --profile full --profile local-model --profile slack -f compose.other.yaml down -v"
        );
    }

    /// The endpoint constants are hardcoded; this asserts they still match the
    /// port mappings in the committed compose file (the "verify against the
    /// file" the task asks for, kept mechanical).
    #[test]
    fn endpoints_match_compose_file() {
        let compose = read_compose("compose.dev.yaml");
        // Each printed host port must appear as a `"<host>:<container>"` mapping.
        for (label, host_port) in [
            ("AgentOS API", "28000"),
            ("AgentOS Console", "28080"),
            ("Langfuse UI", "23000"),
            ("Postgres", "25432"),
            ("Valkey", "26379"),
            ("ClickHouse HTTP", "28123"),
            ("MinIO S3", "29000"),
            ("MinIO console", "29001"),
            ("OTel gRPC", "24317"),
            ("OTel HTTP", "24318"),
        ] {
            assert!(
                compose.contains(&format!("\"{host_port}:")),
                "compose.dev.yaml no longer maps host port {host_port} for {label}"
            );
            assert!(
                ENDPOINTS.iter().any(|(_, url, _)| url.contains(host_port)),
                "ENDPOINTS missing {host_port} for {label}"
            );
        }
        // The console must be advertised in wired mode (?api=1); the published UI
        // image is fixture-by-default and only talks to the API when the URL
        // carries this param.
        let console = ENDPOINTS
            .iter()
            .find(|(label, _, _)| *label == "AgentOS Console")
            .expect("AgentOS Console endpoint present");
        assert!(
            console.1.contains("api=1"),
            "AgentOS Console endpoint must be the wired ?api=1 URL, got {}",
            console.1
        );
    }

    /// The 7 services that must carry `profiles: *core_profiles`.
    const CORE_SERVICES: &[&str] = &[
        "postgres",
        "valkey",
        "minio",
        "minio-init",
        "agentos-migrate",
        "agentos-api",
        "agentos-worker",
    ];

    /// The 5 services that must carry `profiles: *full_profiles`.
    const FULL_SERVICES: &[&str] = &[
        "clickhouse",
        "langfuse-worker",
        "langfuse-web",
        "otel-collector",
        "agentos-ui",
    ];

    /// Return the YAML block for `service`: everything from its `  <service>:`
    /// header up to the next top-level (2-space-indented) service header. Used
    /// to assert a profile anchor lives inside the *right* service block, so a
    /// per-service profile swap fails the test rather than passing on counts.
    fn service_block<'a>(compose: &'a str, service: &str) -> &'a str {
        let header = format!("\n  {service}:\n");
        let start = compose
            .find(&header)
            .unwrap_or_else(|| panic!("service {service} not found"));
        let after = start + header.len();
        let rest = &compose[after..];
        // The next service header is the next "\n  " whose following char is not
        // a space (deeper-indented keys start with "\n    ").
        let end = rest
            .match_indices("\n  ")
            .find(|(i, _)| rest[i + 3..].starts_with(|c: char| c != ' '))
            .map(|(i, _)| i)
            .unwrap_or(rest.len());
        &rest[..end]
    }

    /// Assert the shared core(7)/full(5) profile binding in a compose file:
    /// the anchors are declared, the counts hold, AND each service block carries
    /// the anchor it should (so swapping a service's profile fails the test).
    fn assert_core_full_bindings(compose: &str, file: &str) {
        assert!(
            compose.contains("x-core-profiles: &core_profiles [core, full]"),
            "{file} missing core anchor"
        );
        assert!(
            compose.contains("x-full-profiles: &full_profiles [full]"),
            "{file} missing full anchor"
        );
        assert_eq!(
            compose.matches("profiles: *core_profiles").count(),
            7,
            "{file} core-profile count"
        );
        assert_eq!(
            compose.matches("profiles: *full_profiles").count(),
            5,
            "{file} full-profile count"
        );
        for service in CORE_SERVICES {
            let block = service_block(compose, service);
            assert!(
                block.contains("profiles: *core_profiles"),
                "{file}: {service} block must bind *core_profiles"
            );
            assert!(
                !block.contains("profiles: *full_profiles"),
                "{file}: {service} block must not bind *full_profiles"
            );
        }
        for service in FULL_SERVICES {
            let block = service_block(compose, service);
            assert!(
                block.contains("profiles: *full_profiles"),
                "{file}: {service} block must bind *full_profiles"
            );
            assert!(
                !block.contains("profiles: *core_profiles"),
                "{file}: {service} block must not bind *core_profiles"
            );
        }
    }

    /// Lock which endpoints are advertised under `--minimal`: exactly the five
    /// backed by a `core`-profile service. A core/full mislabel here would print
    /// a dead URL (or hide a live one) under `--minimal`.
    #[test]
    fn minimal_advertises_only_core_endpoints() {
        let core: Vec<&str> = ENDPOINTS
            .iter()
            .filter(|(_, _, is_core)| *is_core)
            .map(|(label, _, _)| *label)
            .collect();
        assert_eq!(
            core,
            vec![
                "AgentOS API",
                "Postgres",
                "Valkey",
                "MinIO S3",
                "MinIO console",
            ]
        );
    }

    #[test]
    fn compose_file_declares_core_and_full_profiles() {
        let compose = read_compose("compose.dev.yaml");
        assert_core_full_bindings(&compose, "compose.dev.yaml");
    }

    #[test]
    fn compose_file_makes_worker_slack_stub_overridable() {
        let compose = read_compose("compose.dev.yaml");
        assert!(compose.contains(
            "      - SLACK_API_BASE_URL=${SLACK_API_BASE_URL-http://localhost:8155/api/}"
        ));
        assert!(compose.contains("      - SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-xoxb-dev}"));
    }

    #[test]
    fn compose_file_declares_slack_dispatcher_profile() {
        let compose = read_compose("compose.dev.yaml");
        let dispatcher = compose
            .split("  agentos-dispatcher:")
            .nth(1)
            .expect("agentos-dispatcher service present");
        assert!(dispatcher.contains("    profiles: [slack]"));
        assert!(!dispatcher.contains("profiles: *core_profiles"));
        assert!(!dispatcher.contains("profiles: *full_profiles"));
        assert!(dispatcher.contains("      VALKEY_HOST: valkey"));
        assert!(dispatcher.contains("      SLACK_APP_TOKEN: ${SLACK_APP_TOKEN:-}"));
    }
}
