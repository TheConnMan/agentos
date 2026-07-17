//! Client for the platform API (apps/api, committed openapi.json contract).
//!
//! `agentos cluster deploy` pushes a local bundle to the platform: find-or-create the
//! agent, create a version, upload the tar.gz bundle (validated server-side by
//! the frozen plugin-format package), and create a deployment. Auth is the
//! X-API-Key header.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::json;

pub struct ApiClient {
    base_url: String,
    api_key: String,
    http: reqwest::Client,
}

/// The channel used when an agent is first created if `--slack-channel` is
/// omitted; on an existing agent an omitted channel is left untouched. Must
/// satisfy the platform API's channel-ID validation (`^[CDG][A-Z0-9]{7,}$`),
/// so this is a valid Slack channel-ID shape, not a `#name`.
pub const DEFAULT_SLACK_CHANNEL: &str = "C0LOCALDEV";

#[derive(Debug, Clone, Deserialize)]
pub struct Agent {
    pub id: String,
    pub name: String,
    pub slack_channel: String,
    /// Tool names gated behind human approval (#245). Present on `AgentOut`;
    /// `#[serde(default)]` keeps older/leaner responses parsing to None.
    #[serde(default)]
    pub approval_required_tools: Option<Vec<String>>,
}

/// One approval record, hand-mirroring the committed `ApprovalOut` (#506). Only
/// the fields the CLI renders are modeled; serde ignores the rest of the payload.
#[derive(Debug, Clone, Deserialize)]
pub struct ApprovalRecord {
    pub id: String,
    pub author: String,
    #[serde(default)]
    pub route: Option<String>,
    #[serde(default)]
    pub gate_kind: Option<String>,
    #[serde(default)]
    pub granted_tool: Option<String>,
    pub status: String,
    pub conversation_id: String,
    pub summary: String,
    #[serde(default)]
    pub expires_at: Option<String>,
    #[serde(default)]
    pub resolved_by: Option<String>,
}

/// What a deploy did with the agent's Slack channel, for the summary printout.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChannelOutcome {
    /// A new agent was created bound to this channel.
    Created(String),
    /// An existing agent's channel was moved.
    Updated { from: String, to: String },
    /// An existing agent's channel was left as-is. `passed` records whether a
    /// `--slack-channel` was supplied (and merely matched) so the caller can hint
    /// how to move it when none was given.
    Unchanged { channel: String, passed: bool },
}

#[derive(Debug, Clone, Deserialize)]
pub struct Version {
    pub id: String,
    pub version_label: String,
    // Extra VersionOut fields for the `versions` listing verb; `#[serde(default)]`
    // keeps the deploy path (which only reads id/version_label) tolerant of a
    // leaner response.
    #[serde(default)]
    pub commit_sha: Option<String>,
    /// The bundle's content hash (`VersionOut.bundle_sha256`): the field that
    /// proves parity — "the artifact running here is the one I tested" (#548).
    /// `#[serde(default)]` keeps the deploy path (which reads only id/label)
    /// tolerant of a leaner response.
    #[serde(default)]
    pub bundle_sha256: Option<String>,
    #[serde(default)]
    pub created_by: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
}

/// A freshly minted console login code (`ConsoleLoginCodeOut`, #630/ADR-0049).
///
/// The `code` is a single-use, short-lived credential the operator pastes into
/// the console, NOT the platform key. It is meant to be printed: that is the
/// whole point of minting it, since it is what lets the browser establish a
/// session without ever receiving the key that authorizes every router.
#[derive(Debug, Clone, Deserialize)]
pub struct ConsoleLoginCode {
    pub code: String,
    pub expires_at: String,
    pub session_id: String,
}

/// One learned memory entry (`MemoryEntryOut`) for the `memory` listing verb.
#[derive(Debug, Clone, Deserialize)]
pub struct MemoryEntry {
    pub index: u64,
    pub content: String,
    pub version: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Bundle {
    pub bundle_ref: String,
    pub bundle_sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Deployment {
    pub id: String,
    pub environment: String,
    pub status: String,
    // Extra DeploymentOut fields used to resolve the in-force version for the
    // `approvals` gate read (#546); `#[serde(default)]` keeps the deploy path
    // (which reads only id/environment/status) tolerant of a leaner response.
    #[serde(default)]
    pub version_id: Option<String>,
    #[serde(default)]
    pub deployed_at: Option<String>,
}

/// One readable text file from a version's stored bundle (`BundleFile` in
/// openapi.json), from `GET /agents/{id}/versions/{version_id}/files`.
#[derive(Debug, Clone, Deserialize)]
pub struct BundleFile {
    pub path: String,
    pub content: String,
}

/// The agent kill-switch state (`KillState` in openapi.json): the response of
/// `POST /agents/{id}/kill` and `POST /agents/{id}/resume`.
#[derive(Debug, Clone, Deserialize)]
pub struct KillState {
    pub killed: bool,
}

/// The enqueued eval job's identity (`EvalTriggerResult` in openapi.json): the
/// response of `POST /evals/trigger`. `sha` keys the run's matrix column and
/// `model` echoes the requested model (#526) so a sweep can pair each job to the
/// row it will produce.
#[derive(Debug, Clone, Deserialize)]
pub struct EvalTriggerResult {
    pub stream_id: String,
    pub sha: String,
    pub suite: String,
    #[serde(default)]
    pub model: Option<String>,
}

/// One per-model rollup of the eval matrix (`EvalModelSummary` in openapi.json):
/// pass-rate and summed cost for a suite run under one model (#255/#526). `model`
/// is `None` for the matrix's unlabelled column (a run with no resolved model).
#[derive(Debug, Clone, Deserialize)]
pub struct EvalModelSummary {
    #[serde(default)]
    pub model: Option<String>,
    pub passed: u64,
    pub total: u64,
    #[serde(default)]
    pub cost_usd: Option<f64>,
}

/// The eval matrix grid (`EvalMatrix` in openapi.json): `GET /evals/matrix`. The
/// sweep reads `model_summaries` (the model dimension) plus `versions` (the shown
/// version columns, newest first): a `--model` sweep uses `versions` to scope
/// readiness to the run it just triggered, so a prior run's rows cannot satisfy
/// the exit condition on the first poll (issue #608). The per-case `rows` grid is
/// carried by the endpoint but unused here.
#[derive(Debug, Clone, Deserialize)]
pub struct EvalMatrix {
    pub suite: String,
    /// The shown version columns (commit shas), most recent first. A triggered
    /// run's sha appears here only once at least one of its traces has landed.
    #[serde(default)]
    pub versions: Vec<String>,
    #[serde(default)]
    pub model_summaries: Vec<EvalModelSummary>,
}

/// The per-agent budget (`BudgetConfig` in openapi.json): the request and
/// response body of `PUT /agents/{id}/budget`. Both fields are optional; an
/// omitted field means "platform default" server-side, so we only serialize the
/// ones the caller set.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BudgetConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_output_tokens_per_run: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_usd_per_day: Option<f64>,
}

/// The artifacts a deploy produces, for the summary printout.
pub struct DeployOutcome {
    pub agent: Agent,
    pub version: Version,
    pub bundle: Bundle,
    pub deployment: Deployment,
    pub channel: ChannelOutcome,
}

/// Whether this endpoint would send the `X-API-Key` over cleartext HTTP to a
/// non-loopback host (a forgotten `https://` that leaks the key on the wire).
/// Local dev over `http://localhost` is expected and returns false.
fn is_insecure_endpoint(base_url: &str) -> bool {
    let lower = base_url.trim().to_ascii_lowercase();
    if lower.starts_with("https://") {
        return false;
    }
    let authority = lower
        .strip_prefix("http://")
        .unwrap_or(&lower)
        .split('/')
        .next()
        .unwrap_or("");
    // Strip the port, handling both `host:port` and `[::1]:port` IPv6 forms.
    let host = if let Some(rest) = authority.strip_prefix('[') {
        rest.split(']').next().unwrap_or("")
    } else {
        authority.split(':').next().unwrap_or("")
    };
    let is_loopback = host == "localhost"
        || host.ends_with(".localhost")
        || host.starts_with("127.")
        || host == "::1"
        || host == "0.0.0.0";
    !is_loopback
}

/// Warn (to stderr) when the endpoint would leak the API key over cleartext
/// HTTP. See [`is_insecure_endpoint`].
fn warn_if_insecure(base_url: &str) {
    if is_insecure_endpoint(base_url) {
        eprintln!(
            "warning: API endpoint '{base_url}' uses cleartext HTTP; the API key \
             will be sent unencrypted. Use an https:// URL for non-local endpoints."
        );
    }
}

impl ApiClient {
    pub fn new(base_url: &str, api_key: &str) -> Result<Self> {
        warn_if_insecure(base_url);
        let http = reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(5))
            .build()
            .context("building HTTP client")?;
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            http,
        })
    }

    async fn expect_ok(resp: reqwest::Response, what: &str) -> Result<reqwest::Response> {
        if resp.status().is_success() {
            return Ok(resp);
        }
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        bail!("{what} failed with {status}: {}", body.trim());
    }

    pub async fn list_agents(&self) -> Result<Vec<Agent>> {
        let resp = self
            .http
            .get(format!("{}/agents", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /agents")?;
        Self::expect_ok(resp, "listing agents")
            .await?
            .json()
            .await
            .context("decoding agent list")
    }

    pub async fn create_agent(&self, name: &str, slack_channel: &str) -> Result<Agent> {
        let resp = self
            .http
            .post(format!("{}/agents", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({"name": name, "slack_channel": slack_channel}))
            .send()
            .await
            .context("POST /agents")?;
        Self::expect_ok(resp, "creating the agent")
            .await?
            .json()
            .await
            .context("decoding created agent")
    }

    pub async fn find_or_create_agent(&self, name: &str, slack_channel: &str) -> Result<Agent> {
        if let Some(existing) = self
            .list_agents()
            .await?
            .into_iter()
            .find(|a| a.name == name)
        {
            return Ok(existing);
        }
        self.create_agent(name, slack_channel).await
    }

    pub async fn update_agent_channel(&self, agent_id: &str, slack_channel: &str) -> Result<Agent> {
        let resp = self
            .http
            .patch(format!("{}/agents/{agent_id}", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({"slack_channel": slack_channel}))
            .send()
            .await
            .context("PATCH /agents/{id}")?;
        Self::expect_ok(resp, "updating the agent channel")
            .await?
            .json()
            .await
            .context("decoding updated agent")
    }

    /// Bind the per-agent connector secrets (ADR-0009, #429). The values travel
    /// in the JSON request body (over the API's X-API-Key channel), never in
    /// argv; the API stores them and returns the agent with names only.
    pub async fn update_agent_secrets(
        &self,
        agent_id: &str,
        secrets: &std::collections::BTreeMap<String, String>,
    ) -> Result<Agent> {
        let resp = self
            .http
            .patch(format!("{}/agents/{agent_id}", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({ "secrets": secrets }))
            .send()
            .await
            .context("PATCH /agents/{id}")?;
        Self::expect_ok(resp, "binding agent connector secrets")
            .await?
            .json()
            .await
            .context("decoding updated agent")
    }

    /// Find the agent by name (or create it), reconciling its Slack channel with
    /// an explicitly-passed `--slack-channel`. A new agent binds to the passed
    /// channel (or the default); an existing agent's channel is moved via PATCH
    /// only when a channel was passed and differs -- an omitted channel never
    /// silently overwrites what is already set.
    async fn resolve_agent(
        &self,
        name: &str,
        slack_channel: Option<&str>,
    ) -> Result<(Agent, ChannelOutcome)> {
        let existing = self
            .list_agents()
            .await?
            .into_iter()
            .find(|a| a.name == name);
        match existing {
            Some(agent) => match slack_channel {
                Some(channel) if channel != agent.slack_channel => {
                    let from = agent.slack_channel.clone();
                    let updated = self.update_agent_channel(&agent.id, channel).await?;
                    let to = updated.slack_channel.clone();
                    Ok((updated, ChannelOutcome::Updated { from, to }))
                }
                other => {
                    let channel = agent.slack_channel.clone();
                    Ok((
                        agent,
                        ChannelOutcome::Unchanged {
                            channel,
                            passed: other.is_some(),
                        },
                    ))
                }
            },
            None => {
                let channel = slack_channel.unwrap_or(DEFAULT_SLACK_CHANNEL);
                let agent = self.create_agent(name, channel).await?;
                let outcome = ChannelOutcome::Created(agent.slack_channel.clone());
                Ok((agent, outcome))
            }
        }
    }

    pub async fn create_version(
        &self,
        agent_id: &str,
        version_label: &str,
        created_by: &str,
    ) -> Result<Version> {
        let resp = self
            .http
            .post(format!("{}/agents/{agent_id}/versions", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({"version_label": version_label, "created_by": created_by}))
            .send()
            .await
            .context("POST /agents/{id}/versions")?;
        Self::expect_ok(resp, "creating the version")
            .await?
            .json()
            .await
            .context("decoding created version")
    }

    pub async fn upload_bundle(
        &self,
        agent_id: &str,
        version_id: &str,
        archive: Vec<u8>,
    ) -> Result<Bundle> {
        let part = reqwest::multipart::Part::bytes(archive)
            .file_name("bundle.tar.gz")
            .mime_str("application/gzip")
            .context("building multipart body")?;
        let form = reqwest::multipart::Form::new().part("file", part);
        let resp = self
            .http
            .put(format!(
                "{}/agents/{agent_id}/versions/{version_id}/bundle",
                self.base_url
            ))
            .header("X-API-Key", &self.api_key)
            .multipart(form)
            .send()
            .await
            .context("PUT bundle")?;
        Self::expect_ok(resp, "uploading the bundle")
            .await?
            .json()
            .await
            .context("decoding bundle result")
    }

    pub async fn create_deployment(
        &self,
        agent_id: &str,
        version_id: &str,
        environment: &str,
    ) -> Result<Deployment> {
        let resp = self
            .http
            .post(format!("{}/deployments", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({
                "agent_id": agent_id,
                "version_id": version_id,
                "environment": environment,
            }))
            .send()
            .await
            .context("POST /deployments")?;
        Self::expect_ok(resp, "creating the deployment")
            .await?
            .json()
            .await
            .context("decoding created deployment")
    }

    /// The full deploy flow: resolve agent (create or channel-reconcile),
    /// version, bundle, deployment.
    #[allow(clippy::too_many_arguments)] // one cohesive deploy call; a struct would not clarify it
    pub async fn deploy(
        &self,
        agent_name: &str,
        slack_channel: Option<&str>,
        version_label: &str,
        created_by: &str,
        environment: &str,
        archive: Vec<u8>,
        secrets: &std::collections::BTreeMap<String, String>,
    ) -> Result<DeployOutcome> {
        let (agent, channel) = self.resolve_agent(agent_name, slack_channel).await?;
        // Bind per-agent connector secrets (ADR-0009, #429). A PATCH covers both
        // a freshly created agent and a redeploy that rotates a value; an empty
        // map leaves the agent's current secrets untouched.
        if !secrets.is_empty() {
            self.update_agent_secrets(&agent.id, secrets).await?;
        }
        let version = self
            .create_version(&agent.id, version_label, created_by)
            .await?;
        let bundle = self.upload_bundle(&agent.id, &version.id, archive).await?;
        let deployment = self
            .create_deployment(&agent.id, &version.id, environment)
            .await?;
        Ok(DeployOutcome {
            agent,
            version,
            bundle,
            deployment,
            channel,
        })
    }

    /// Resolve an agent identifier (its `name`, or its `id`) to the full record
    /// by listing agents and matching -- the same name-based resolution the
    /// deploy flow uses (`resolve_agent`), so the lifecycle verbs never grow a
    /// second resolution path. Errors when nothing matches; never creates.
    pub async fn find_agent(&self, identifier: &str) -> Result<Agent> {
        self.list_agents()
            .await?
            .into_iter()
            .find(|a| a.name == identifier || a.id == identifier)
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "no agent found matching {identifier:?} (by name or id); deploy it first with `agentos cluster deploy`"
                )
            })
    }

    /// Flip the agent kill switch on: `POST /agents/{id}/kill` (no request body).
    pub async fn kill_agent(&self, agent_id: &str) -> Result<KillState> {
        let resp = self
            .http
            .post(format!("{}/agents/{agent_id}/kill", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("POST /agents/{id}/kill")?;
        Self::expect_ok(resp, "killing the agent")
            .await?
            .json()
            .await
            .context("decoding kill state")
    }

    /// Flip the agent kill switch off: `POST /agents/{id}/resume` (no request body).
    pub async fn resume_agent(&self, agent_id: &str) -> Result<KillState> {
        let resp = self
            .http
            .post(format!("{}/agents/{agent_id}/resume", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("POST /agents/{id}/resume")?;
        Self::expect_ok(resp, "resuming the agent")
            .await?
            .json()
            .await
            .context("decoding kill state")
    }

    /// Set the agent budget: `PUT /agents/{id}/budget` with a `BudgetConfig` body.
    pub async fn set_budget(&self, agent_id: &str, budget: &BudgetConfig) -> Result<BudgetConfig> {
        let resp = self
            .http
            .put(format!("{}/agents/{agent_id}/budget", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(budget)
            .send()
            .await
            .context("PUT /agents/{id}/budget")?;
        Self::expect_ok(resp, "updating the budget")
            .await?
            .json()
            .await
            .context("decoding budget")
    }

    /// List an agent's immutable versions, ascending by `created_at` (oldest
    /// first): `GET /agents/{id}/versions`. `commands::versions` reverses
    /// this to newest-first before display/JSON output.
    pub async fn list_versions(&self, agent_id: &str) -> Result<Vec<Version>> {
        let resp = self
            .http
            .get(format!("{}/agents/{agent_id}/versions", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /agents/{id}/versions")?;
        Self::expect_ok(resp, "listing versions")
            .await?
            .json()
            .await
            .context("decoding version list")
    }

    /// List an agent's learned memory, oldest first: `GET /agents/{id}/memory`.
    pub async fn list_memory(&self, agent_id: &str) -> Result<Vec<MemoryEntry>> {
        let resp = self
            .http
            .get(format!("{}/agents/{agent_id}/memory", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /agents/{id}/memory")?;
        Self::expect_ok(resp, "listing memory")
            .await?
            .json()
            .await
            .context("decoding memory list")
    }

    /// The pending approval records for an agent: `GET /approvals?status_filter=
    /// pending&agent_id=<id>`. Hand-mirrors the committed `ApprovalOut` shape
    /// (only the fields the CLI renders; serde ignores the rest), the same way
    /// `Agent`/`KillState` mirror `openapi.json` (#506).
    pub async fn list_pending_approvals(&self, agent_id: &str) -> Result<Vec<ApprovalRecord>> {
        let resp = self
            .http
            .get(format!("{}/approvals", self.base_url))
            .header("X-API-Key", &self.api_key)
            .query(&[("status_filter", "pending"), ("agent_id", agent_id)])
            .send()
            .await
            .context("GET /approvals")?;
        Self::expect_ok(resp, "listing pending approvals")
            .await?
            .json()
            .await
            .context("decoding approvals")
    }

    /// Resolve one approval as a chosen actor: `POST /approvals/{id}/resolve`.
    /// The server owns the resolve-once CAS, the authorizer (self-approval block,
    /// route approvers), and the resume-turn enqueue; `resolved_by` is the acting
    /// actor (the `--as` flag), which is what makes requester != approver
    /// expressible without hand-curling the API (#506).
    pub async fn resolve_approval(
        &self,
        approval_id: &str,
        decision: &str,
        resolved_by: &str,
        note: Option<&str>,
    ) -> Result<ApprovalRecord> {
        let mut body = json!({ "decision": decision, "resolved_by": resolved_by });
        if let Some(note) = note {
            body["note"] = json!(note);
        }
        let resp = self
            .http
            .post(format!("{}/approvals/{approval_id}/resolve", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&body)
            .send()
            .await
            .context("POST /approvals/{id}/resolve")?;
        Self::expect_ok(resp, "resolving approval")
            .await?
            .json()
            .await
            .context("decoding resolved approval")
    }

    /// Set the agent's approval-required tool gates: `PATCH /agents/{id}` with
    /// `approval_required_tools` (an empty list clears them). Returns the updated
    /// agent so the caller can echo the effective gates.
    pub async fn set_approval_tools(&self, agent_id: &str, tools: &[String]) -> Result<Agent> {
        let resp = self
            .http
            .patch(format!("{}/agents/{agent_id}", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({ "approval_required_tools": tools }))
            .send()
            .await
            .context("PATCH /agents/{id} (approval gates)")?;
        Self::expect_ok(resp, "updating approval gates")
            .await?
            .json()
            .await
            .context("decoding updated agent")
    }

    /// Enqueue an on-demand platform eval run: `POST /evals/trigger`. With no
    /// `version_id` the agent's active dev deployment is evaluated. `model` (#526)
    /// pins the run's model dimension so a sweep posts one trigger per model and
    /// reads the comparison back off the matrix. Returns the enqueued job identity.
    pub async fn trigger_eval(
        &self,
        agent_id: &str,
        suite: Option<&str>,
        model: Option<&str>,
    ) -> Result<EvalTriggerResult> {
        let mut body = json!({ "agent_id": agent_id });
        if let Some(suite) = suite {
            body["suite"] = json!(suite);
        }
        if let Some(model) = model {
            body["model"] = json!(model);
        }
        let resp = self
            .http
            .post(format!("{}/evals/trigger", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&body)
            .send()
            .await
            .context("POST /evals/trigger")?;
        Self::expect_ok(resp, "triggering the eval")
            .await?
            .json()
            .await
            .context("decoding eval trigger result")
    }

    /// Read the eval matrix for a suite: `GET /evals/matrix?suite=..&versions=..`.
    /// The sweep polls this for the per-model pass-rate rollup the recorder writes.
    pub async fn eval_matrix(&self, suite: &str, versions: u32) -> Result<EvalMatrix> {
        let resp = self
            .http
            .get(format!("{}/evals/matrix", self.base_url))
            .query(&[("suite", suite), ("versions", &versions.to_string())])
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /evals/matrix")?;
        Self::expect_ok(resp, "reading the eval matrix")
            .await?
            .json()
            .await
            .context("decoding eval matrix")
    }

    /// List an agent's deployments, oldest first: `GET /deployments?agent_id={id}`.
    /// Used to resolve the in-force version whose bundle manifest gates the
    /// `approvals` read must union in (#546).
    pub async fn list_deployments(&self, agent_id: &str) -> Result<Vec<Deployment>> {
        let resp = self
            .http
            .get(format!("{}/deployments", self.base_url))
            .query(&[("agent_id", agent_id)])
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /deployments")?;
        Self::expect_ok(resp, "listing deployments")
            .await?
            .json()
            .await
            .context("decoding deployment list")
    }

    /// Read a version's authored text files (skills, manifest, eval cases):
    /// `GET /agents/{id}/versions/{version_id}/files`. The `approvals` read pulls
    /// the deployed bundle's manifest from here to recover its `approvalPolicy`
    /// gates (#546).
    pub async fn bundle_files(&self, agent_id: &str, version_id: &str) -> Result<Vec<BundleFile>> {
        #[derive(serde::Deserialize)]
        struct BundleFiles {
            files: Vec<BundleFile>,
        }
        let resp = self
            .http
            .get(format!(
                "{}/agents/{agent_id}/versions/{version_id}/files",
                self.base_url
            ))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("GET /agents/{id}/versions/{version_id}/files")?;
        let files: BundleFiles = Self::expect_ok(resp, "reading bundle files")
            .await?
            .json()
            .await
            .context("decoding bundle files")?;
        Ok(files.files)
    }

    /// Delete the agent: `DELETE /agents/{id}` (204 No Content on success).
    pub async fn delete_agent(&self, agent_id: &str) -> Result<()> {
        let resp = self
            .http
            .delete(format!("{}/agents/{agent_id}", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("DELETE /agents/{id}")?;
        Self::expect_ok(resp, "deleting the agent").await?;
        Ok(())
    }

    /// Mint a single-use console login code: `POST /console/login-codes`
    /// (#630/ADR-0049). Under the platform key on purpose -- minting is the
    /// CLI's job, which is what keeps the browser off the key entirely.
    pub async fn mint_console_login_code(&self, label: Option<&str>) -> Result<ConsoleLoginCode> {
        let resp = self
            .http
            .post(format!("{}/console/login-codes", self.base_url))
            .header("X-API-Key", &self.api_key)
            .json(&json!({ "label": label }))
            .send()
            .await
            .context("POST /console/login-codes")?;
        Self::expect_ok(resp, "minting a console login code")
            .await?
            .json()
            .await
            .context("decoding the minted login code")
    }

    /// Revoke every live console grant: `DELETE /console/sessions`, returning
    /// how many rows were revoked. The operator's kill switch for the console.
    pub async fn revoke_console_sessions(&self) -> Result<u64> {
        #[derive(Deserialize)]
        struct Revoked {
            revoked: u64,
        }
        let resp = self
            .http
            .delete(format!("{}/console/sessions", self.base_url))
            .header("X-API-Key", &self.api_key)
            .send()
            .await
            .context("DELETE /console/sessions")?;
        let out: Revoked = Self::expect_ok(resp, "revoking console sessions")
            .await?
            .json()
            .await
            .context("decoding the revoke result")?;
        Ok(out.revoked)
    }
}

#[cfg(test)]
mod tests {
    use super::is_insecure_endpoint;

    #[test]
    fn https_is_always_secure() {
        assert!(!is_insecure_endpoint("https://api.example.com"));
        assert!(!is_insecure_endpoint("HTTPS://API.EXAMPLE.COM"));
    }

    #[test]
    fn http_to_loopback_is_allowed() {
        for url in [
            "http://localhost:8000",
            "http://localhost",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
            "http://0.0.0.0:8000",
            "http://api.localhost",
        ] {
            assert!(!is_insecure_endpoint(url), "expected {url} to be allowed");
        }
    }

    #[test]
    fn http_to_remote_host_is_insecure() {
        for url in [
            "http://api.example.com",
            "http://api.example.com:8000/v1",
            "http://10.0.0.5:8000",
        ] {
            assert!(is_insecure_endpoint(url), "expected {url} to warn");
        }
    }
}
