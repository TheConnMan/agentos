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
        "postgresql+asyncpg://postgres:postgres@localhost:55434/postgres"
    )
    db_schema: str = "agentos"

    # Single shared API key. Dev-only default; override in any shared deployment.
    api_key: str = "agentos-dev-key"

    # Langfuse proxy target (the dev project keys baked into compose.dev.yaml).
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = "pk-lf-agentos-dev"
    langfuse_secret_key: str = "sk-lf-agentos-dev"

    # MinIO / S3 for immutable plugin bundles (compose stack MinIO on 9002).
    s3_endpoint_url: str = "http://localhost:9002"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_region: str = "us-east-1"
    bundle_bucket: str = "agentos-bundles"

    # Git flow (J1). The webhook secret authenticates inbound GitHub events; the
    # two bot identities are the routing targets recorded on each deployment.
    github_webhook_secret: str = "dev-webhook-secret"
    dev_branch: str = "dev"
    prod_branch: str = "main"
    bot_identity_dev: str = "@agentos-dev"
    bot_identity_prod: str = "@agentos"
    # Clone-URL schemes the git-flow builder will fetch from. file:// supports
    # the hermetic local-bare-repo tests; anything else (e.g. git ext::) is
    # refused before a subprocess runs.
    git_allowed_schemes: tuple[str, ...] = ("file://", "https://", "http://")

    # Observability (OB1). kube_config_path points the runner-logs proxy at a
    # cluster; when unset the API tries in-cluster config, and if neither is
    # available the logs endpoint degrades to 503 rather than crashing.
    kube_config_path: str | None = None
    metrics_default_window_hours: int = 168  # 7 days


@lru_cache
def get_settings() -> Settings:
    return Settings()
