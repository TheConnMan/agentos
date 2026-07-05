//! Client for the platform API (apps/api, committed openapi.json contract).
//!
//! `agentos deploy` pushes a local bundle to the platform: find-or-create the
//! agent, create a version, upload the tar.gz bundle (validated server-side by
//! the frozen plugin-format package), and create a deployment. Auth is the
//! X-API-Key header.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::json;

pub struct ApiClient {
    base_url: String,
    api_key: String,
    http: reqwest::Client,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Agent {
    pub id: String,
    pub name: String,
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

/// The artifacts a deploy produces, for the summary printout.
pub struct DeployOutcome {
    pub agent: Agent,
    pub version: Version,
    pub bundle: Bundle,
    pub deployment: Deployment,
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

    /// The full deploy flow: find-or-create agent, version, bundle, deployment.
    pub async fn deploy(
        &self,
        agent_name: &str,
        slack_channel: &str,
        version_label: &str,
        created_by: &str,
        environment: &str,
        archive: Vec<u8>,
    ) -> Result<DeployOutcome> {
        let agent = self.find_or_create_agent(agent_name, slack_channel).await?;
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
        })
    }
}
