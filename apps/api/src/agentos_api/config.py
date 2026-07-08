"""Runtime configuration for the AgentOS API server.

All values default to the compose dev stack (see compose.dev.yaml and
.env.example) so a local run needs no .env. Override any field via the matching
environment variable for shared or production deployments.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres (async driver). Dedicated `agentos` schema keeps our tables clear
    # of Langfuse's own tables on the same database.
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
    )
    db_schema: str = "agentos"

    # Single shared API key. Dev-only default; override in any shared deployment.
    api_key: str = "agentos-dev-key"

    # Human-readable org/workspace name the UI reads (open /config endpoint) to
    # brand the app. Overridable via ORG_NAME for a white-labeled deployment.
    org_name: str = "AgentOS"

    # Langfuse proxy target (the dev project keys baked into compose.dev.yaml).
    langfuse_host: str = "http://localhost:23000"
    langfuse_public_key: str = "pk-lf-agentos-dev"
    langfuse_secret_key: str = "sk-lf-agentos-dev"

    # MinIO / S3 for immutable plugin bundles (compose stack MinIO on 29000).
    s3_endpoint_url: str = "http://localhost:29000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_region: str = "us-east-1"
    bundle_bucket: str = "agentos-bundles"

    # Git flow (J1). The webhook secret authenticates inbound GitHub events; the
    # two bot identities are the routing targets recorded on each deployment.
    github_webhook_secret: str = "dev-webhook-secret"
    dev_branch: str = "dev"
    prod_branch: str = "main"
    # Outbound GitHub commit-status API for the eval PR check (K1).
    github_api_url: str = "https://api.github.com"
    github_token: str = ""
    eval_check_context: str = "agentos/evals"
    # Suite name put on the fan-out request for a dev-push eval run (the plugin
    # bundle carries the suite itself; the consumer resolves it by this name).
    eval_default_suite: str = "default"
    bot_identity_dev: str = "@agentos-dev"
    bot_identity_prod: str = "@agentos"
    # Clone-URL schemes the git-flow builder will fetch from. file:// supports
    # the hermetic local-bare-repo tests; anything else (e.g. git ext::) is
    # refused before a subprocess runs.
    git_allowed_schemes: tuple[str, ...] = ("file://", "https://", "http://")

    # Valkey for the kill switch (L1): SET the flag + PUBLISH the kill event.
    # The DSN is built from the parts so the compose VALKEY_PASSWORD override is
    # honored; set valkey_url to override the whole DSN (e.g. TLS, other host).
    valkey_password: str = "valkeypass"
    valkey_host: str = "localhost"
    valkey_port: int = 26379
    valkey_url: str | None = None

    # Observability (OB1). kube_config_path points the runner-logs proxy at a
    # cluster; when unset the API tries in-cluster config, and if neither is
    # available the logs endpoint degrades to 503 rather than crashing.
    kube_config_path: str | None = None
    metrics_default_window_hours: int = 168  # 7 days
    # The namespace the runner sandboxes run in, and the label selector that
    # identifies them (the chart labels sandbox pods
    # app.kubernetes.io/component=runner-sandbox). Used by the pod-list endpoint
    # that populates the Logs tab's pod dropdown.
    runner_namespace: str = "agentos"
    runner_pod_label_selector: str = "app.kubernetes.io/component=runner-sandbox"

    def valkey_dsn(self) -> str:
        if self.valkey_url:
            return self.valkey_url
        return (
            f"redis://:{self.valkey_password}@{self.valkey_host}:{self.valkey_port}/0"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
