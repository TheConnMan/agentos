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
/// omitted; on an existing agent an omitted channel is left untouched.
pub const DEFAULT_SLACK_CHANNEL: &str = "#local-dev";

#[derive(Debug, Clone, Deserialize)]
pub struct Agent {
    pub id: String,
    pub name: String,
    pub slack_channel: String,
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
}

/// The agent kill-switch state (`KillState` in openapi.json): the response of
/// `POST /agents/{id}/kill` and `POST /agents/{id}/resume`.
#[derive(Debug, Clone, Deserialize)]
pub struct KillState {
    pub killed: bool,
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

impl ApiClient {
    pub fn new(base_url: &str, api_key: &str) -> Result<Self> {
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
    pub async fn deploy(
        &self,
        agent_name: &str,
        slack_channel: Option<&str>,
        version_label: &str,
        created_by: &str,
        environment: &str,
        archive: Vec<u8>,
    ) -> Result<DeployOutcome> {
        let (agent, channel) = self.resolve_agent(agent_name, slack_channel).await?;
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
}
