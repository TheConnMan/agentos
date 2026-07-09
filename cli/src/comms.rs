//! `agentos cluster comms`: wire or clear the cluster's real Slack surface
//! with one `helm upgrade --reuse-values`, keeping the chart as the source of
//! truth.

use anyhow::{bail, Result};

use crate::ops::{plain, require_on_path, run_step, secret_set, CommonOpts, OpsCommand};

/// The worker's Slack stub sink URL (compose default); restored on disconnect.
const LOCAL_SLACK_STUB_URL: &str = "http://localhost:8155/api/";
/// The worker's stub bot token (compose default); restored on disconnect.
const LOCAL_SLACK_STUB_BOT_TOKEN: &str = "xoxb-dev";

#[derive(Debug, Clone)]
pub struct CommsOpts {
    pub common: CommonOpts,
    pub chart: String,
    pub app_token: String,
    pub bot_token: String,
    pub disconnect: bool,
}

pub struct LocalCommsOpts {
    pub file: String,
    pub dry_run: bool,
    pub app_token: String,
    pub bot_token: String,
    pub disconnect: bool,
}

pub fn connect_commands(opts: &CommsOpts) -> Vec<OpsCommand> {
    vec![OpsCommand::new(
        "helm",
        vec![
            plain("upgrade"),
            plain(&opts.common.release),
            plain(&opts.chart),
            plain("-n"),
            plain(&opts.common.namespace),
            plain("--reuse-values"),
            plain("--set"),
            secret_set("dispatcher.slack.appToken", &opts.app_token),
            plain("--set"),
            secret_set("dispatcher.slack.botToken", &opts.bot_token),
            plain("--set"),
            plain("worker.slackApiBaseUrl="),
        ],
    )]
}

pub fn disconnect_commands(opts: &CommsOpts) -> Vec<OpsCommand> {
    vec![OpsCommand::new(
        "helm",
        vec![
            plain("upgrade"),
            plain(&opts.common.release),
            plain(&opts.chart),
            plain("-n"),
            plain(&opts.common.namespace),
            plain("--reuse-values"),
            plain("--set"),
            plain("dispatcher.slack.appToken="),
            plain("--set"),
            plain("dispatcher.slack.botToken="),
        ],
    )]
}

pub fn local_connect_commands(o: &LocalCommsOpts) -> Vec<OpsCommand> {
    vec![OpsCommand::new(
        "docker",
        vec![
            plain("compose"),
            plain("--profile"),
            plain("core"),
            plain("--profile"),
            plain("slack"),
            plain("-f"),
            plain(&o.file),
            plain("up"),
            plain("-d"),
            plain("--wait"),
            plain("agentos-worker"),
            plain("agentos-dispatcher"),
        ],
    )
    .with_env(vec![("SLACK_API_BASE_URL".into(), String::new())])
    .with_secret_env(vec![
        ("SLACK_APP_TOKEN".into(), o.app_token.clone()),
        ("SLACK_BOT_TOKEN".into(), o.bot_token.clone()),
    ])]
}

pub fn local_disconnect_commands(o: &LocalCommsOpts) -> Vec<OpsCommand> {
    vec![
        OpsCommand::new(
            "docker",
            vec![
                plain("compose"),
                plain("--profile"),
                plain("core"),
                plain("--profile"),
                plain("slack"),
                plain("-f"),
                plain(&o.file),
                plain("stop"),
                plain("agentos-dispatcher"),
            ],
        ),
        OpsCommand::new(
            "docker",
            vec![
                plain("compose"),
                plain("--profile"),
                plain("core"),
                plain("-f"),
                plain(&o.file),
                plain("up"),
                plain("-d"),
                plain("--wait"),
                plain("agentos-worker"),
            ],
        )
        .with_env(vec![
            ("SLACK_API_BASE_URL".into(), LOCAL_SLACK_STUB_URL.into()),
            ("SLACK_BOT_TOKEN".into(), LOCAL_SLACK_STUB_BOT_TOKEN.into()),
        ]),
    ]
}

/// `kubectl -n <ns> rollout restart deployment/<release>-<component>`: force the
/// pods to pick up the new Secret-backed Slack tokens (secretKeyRef env vars are
/// resolved once at pod start, so a Secret change alone does not roll them).
fn rollout_restart_command(namespace: &str, release: &str, component: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("-n"),
            plain(namespace),
            plain("rollout"),
            plain("restart"),
            plain(format!("deployment/{release}-{component}")),
        ],
    )
}

/// `kubectl -n <ns> rollout status deployment/<release>-<component> --timeout=120s`:
/// wait for the restarted pods to become ready before reporting success.
fn rollout_status_command(namespace: &str, release: &str, component: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("-n"),
            plain(namespace),
            plain("rollout"),
            plain("status"),
            plain(format!("deployment/{release}-{component}")),
            plain("--timeout=120s"),
        ],
    )
}

/// The kubectl rollout commands that follow the helm upgrade so the running pods
/// pick up the token change. Connect must roll the worker AND the dispatcher (an
/// existing dispatcher would otherwise keep stale tokens; a freshly rendered one
/// is rolled harmlessly). Disconnect rolls only the worker -- helm deletes the
/// dispatcher (its gate `agentos.dispatcher.enabled` goes false), so there is no
/// dispatcher to wait on.
pub fn rollout_commands(disconnect: bool, namespace: &str, release: &str) -> Vec<OpsCommand> {
    let components: &[&str] = if disconnect {
        &["worker"]
    } else {
        &["worker", "dispatcher"]
    };
    let mut cmds: Vec<OpsCommand> = components
        .iter()
        .map(|c| rollout_restart_command(namespace, release, c))
        .collect();
    cmds.extend(
        components
            .iter()
            .map(|c| rollout_status_command(namespace, release, c)),
    );
    cmds
}

/// Exactly one chat surface must be selected. `--slack` is the only surface
/// today; a future surface adds another flag and this check widens.
pub fn require_provider(slack: bool) -> Result<()> {
    if !slack {
        bail!("specify a chat surface, e.g. --slack");
    }
    Ok(())
}

/// On connect, both Slack tokens must be present (from env or the explicit
/// flags). Disconnect needs no tokens, so it always passes.
pub fn require_connect_tokens(disconnect: bool, app_token: &str, bot_token: &str) -> Result<()> {
    if !disconnect && (app_token.is_empty() || bot_token.is_empty()) {
        bail!(
            "Slack tokens missing; set SLACK_APP_TOKEN and SLACK_BOT_TOKEN (or pass --app-token/--bot-token)"
        );
    }
    Ok(())
}

pub async fn comms(opts: CommsOpts) -> Result<()> {
    let ui = crate::ui::ui();
    require_connect_tokens(opts.disconnect, &opts.app_token, &opts.bot_token)?;

    let cmds = if opts.disconnect {
        disconnect_commands(&opts)
    } else {
        connect_commands(&opts)
    };
    let rollout = rollout_commands(
        opts.disconnect,
        &opts.common.namespace,
        &opts.common.release,
    );

    if opts.common.dry_run {
        for cmd in &cmds {
            ui.payload_plain(&cmd.display());
        }
        for cmd in &rollout {
            ui.payload_plain(&cmd.display());
        }
        return Ok(());
    }

    require_on_path("helm")?;
    require_on_path("kubectl")?;
    let cl = ui.checklist();
    let label = if opts.disconnect {
        format!("disconnecting Slack from release {}", opts.common.release)
    } else {
        format!("connecting Slack to release {}", opts.common.release)
    };
    let ok_detail = if opts.disconnect {
        "disconnected"
    } else {
        "connected"
    };
    for cmd in &cmds {
        run_step(&cl, &label, ok_detail, cmd).await?;
    }
    // The Secret change alone does not roll the pods (secretKeyRef env vars are
    // resolved once at pod start), so restart them and wait for the new/cleared
    // token to be live before reporting success.
    let roll_label = format!("rolling {} to pick up tokens", opts.common.release);
    for cmd in &rollout {
        run_step(&cl, &roll_label, "rolled", cmd).await?;
    }
    if opts.disconnect {
        ui.note("Slack disconnected; dispatcher tokens cleared");
    } else {
        ui.note("Slack connected");
    }
    Ok(())
}

pub async fn local_comms(opts: LocalCommsOpts) -> Result<()> {
    let ui = crate::ui::ui();
    require_connect_tokens(opts.disconnect, &opts.app_token, &opts.bot_token)?;
    let cmds = if opts.disconnect {
        local_disconnect_commands(&opts)
    } else {
        local_connect_commands(&opts)
    };

    if opts.dry_run {
        for cmd in &cmds {
            ui.payload_plain(&cmd.display());
        }
        return Ok(());
    }

    require_on_path("docker")?;
    let cl = ui.checklist();
    let label = if opts.disconnect {
        "disconnecting Slack from the local stack"
    } else {
        "connecting Slack to the local stack"
    };
    let ok_detail = if opts.disconnect {
        "disconnected"
    } else {
        "connected"
    };
    for cmd in &cmds {
        run_step(&cl, label, ok_detail, cmd).await?;
    }
    if opts.disconnect {
        ui.note("Slack disconnected; worker back on the local stub");
    } else {
        ui.note("Slack connected to the local stack (dispatcher running, worker on real Slack)");
    }
    Ok(())
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
    fn require_provider_ok_only_with_a_surface() {
        assert!(require_provider(true).is_ok());
        let err = require_provider(false).unwrap_err().to_string();
        assert!(err.contains("specify a chat surface"), "{err}");
    }

    #[test]
    fn require_connect_tokens_guards_connect_but_not_disconnect() {
        assert!(require_connect_tokens(true, "", "").is_ok());
        assert!(require_connect_tokens(false, "xapp-1", "xoxb-1").is_ok());
        let missing_app = require_connect_tokens(false, "", "xoxb-1")
            .unwrap_err()
            .to_string();
        assert!(
            missing_app.contains("Slack tokens missing"),
            "{missing_app}"
        );
        let missing_bot = require_connect_tokens(false, "xapp-1", "")
            .unwrap_err()
            .to_string();
        assert!(
            missing_bot.contains("Slack tokens missing"),
            "{missing_bot}"
        );
    }

    #[test]
    fn connect_command_sets_tokens_and_clears_stub_wiring() {
        let cmds = connect_commands(&CommsOpts {
            common: common(),
            chart: "charts/agentos".into(),
            app_token: "xapp-123456789".into(),
            bot_token: "xoxb-123456789".into(),
            disconnect: false,
        });
        assert_eq!(cmds.len(), 1);
        assert_eq!(
            cmds[0].display(),
            "helm upgrade agentos charts/agentos -n agentos --reuse-values \
             --set 'dispatcher.slack.appToken=xapp-123***' \
             --set 'dispatcher.slack.botToken=xoxb-123***' \
             --set worker.slackApiBaseUrl="
        );
    }

    #[test]
    fn connect_display_masks_both_tokens_but_argv_keeps_raw_values() {
        let cmds = connect_commands(&CommsOpts {
            common: common(),
            chart: "charts/agentos".into(),
            app_token: "xapp-1-secretsecret".into(),
            bot_token: "xoxb-1-secretsecret".into(),
            disconnect: false,
        });
        let line = cmds[0].display();
        assert!(
            line.contains("dispatcher.slack.appToken=xapp-1-s***"),
            "{line}"
        );
        assert!(
            line.contains("dispatcher.slack.botToken=xoxb-1-s***"),
            "{line}"
        );
        assert!(!line.contains("secretsecret"), "secret leaked: {line}");

        let argv = cmds[0].argv().join(" ");
        assert!(
            argv.contains("dispatcher.slack.appToken=xapp-1-secretsecret"),
            "{argv}"
        );
        assert!(
            argv.contains("dispatcher.slack.botToken=xoxb-1-secretsecret"),
            "{argv}"
        );
    }

    #[test]
    fn rollout_commands_connect_rolls_worker_and_dispatcher() {
        let cmds = rollout_commands(false, "agentos", "agentos");
        let lines: Vec<String> = cmds.iter().map(OpsCommand::display).collect();
        assert_eq!(
            lines,
            vec![
                "kubectl -n agentos rollout restart deployment/agentos-worker".to_string(),
                "kubectl -n agentos rollout restart deployment/agentos-dispatcher".to_string(),
                "kubectl -n agentos rollout status deployment/agentos-worker --timeout=120s"
                    .to_string(),
                "kubectl -n agentos rollout status deployment/agentos-dispatcher --timeout=120s"
                    .to_string(),
            ]
        );
    }

    #[test]
    fn rollout_commands_disconnect_rolls_worker_only() {
        let cmds = rollout_commands(true, "agentos", "agentos");
        let lines: Vec<String> = cmds.iter().map(OpsCommand::display).collect();
        assert_eq!(
            lines,
            vec![
                "kubectl -n agentos rollout restart deployment/agentos-worker".to_string(),
                "kubectl -n agentos rollout status deployment/agentos-worker --timeout=120s"
                    .to_string(),
            ]
        );
        assert!(
            !lines.iter().any(|l| l.contains("dispatcher")),
            "disconnect must not touch the dispatcher: {lines:?}"
        );
    }

    #[test]
    fn disconnect_command_clears_tokens_without_stub_url_or_secret_bytes() {
        let cmds = disconnect_commands(&CommsOpts {
            common: common(),
            chart: "charts/agentos".into(),
            app_token: "xapp-1-secretsecret".into(),
            bot_token: "xoxb-1-secretsecret".into(),
            disconnect: true,
        });
        let line = cmds[0].display();
        assert_eq!(
            line,
            "helm upgrade agentos charts/agentos -n agentos --reuse-values \
             --set dispatcher.slack.appToken= --set dispatcher.slack.botToken="
        );
        assert!(!line.contains("worker.slackApiBaseUrl"), "{line}");
        assert!(!line.contains("xapp-"), "{line}");
        assert!(!line.contains("xoxb-"), "{line}");
    }

    #[test]
    fn local_connect_command_wires_dispatcher_and_unwires_stub() {
        let cmds = local_connect_commands(&LocalCommsOpts {
            file: "compose.dev.yaml".into(),
            dry_run: false,
            app_token: "xapp-1-secretsecret".into(),
            bot_token: "xoxb-1-secretsecret".into(),
            disconnect: false,
        });
        assert_eq!(cmds.len(), 1);
        let line = cmds[0].display();
        assert_eq!(
            line,
            "SLACK_API_BASE_URL= 'SLACK_APP_TOKEN=xapp-1-s***' \
             'SLACK_BOT_TOKEN=xoxb-1-s***' \
             docker compose --profile core --profile slack -f compose.dev.yaml up -d --wait \
             agentos-worker agentos-dispatcher"
        );
        assert!(!line.contains("secretsecret"), "secret leaked: {line}");
    }

    #[test]
    fn local_disconnect_commands_stop_dispatcher_and_restub_worker() {
        let cmds = local_disconnect_commands(&LocalCommsOpts {
            file: "compose.dev.yaml".into(),
            dry_run: false,
            app_token: String::new(),
            bot_token: String::new(),
            disconnect: true,
        });
        assert_eq!(cmds.len(), 2);
        assert_eq!(
            cmds[0].display(),
            "docker compose --profile core --profile slack -f compose.dev.yaml stop agentos-dispatcher"
        );
        let second = cmds[1].display();
        assert_eq!(
            second,
            "SLACK_API_BASE_URL=http://localhost:8155/api/ \
             SLACK_BOT_TOKEN=xoxb-dev \
             docker compose --profile core -f compose.dev.yaml up -d --wait agentos-worker"
        );
        assert!(
            !second.contains("agentos-dispatcher"),
            "disconnect worker restart must not mention dispatcher: {second}"
        );
    }
}
