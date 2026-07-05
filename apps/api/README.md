# apps/api

Owning tasks: **B1** (API server core), **B2** (plugin bundle pipeline), **J1** (GitHub App / promote-on-merge), **OB1** (Metrics + Logs observability). FastAPI server: agents/versions/deployments CRUD, auth, plugin bundle validate/store/fetch, Langfuse proxy endpoints, GitHub App integration, and the Langfuse-backed metrics/logs endpoints. Backed by Postgres and MinIO/S3 from the dev compose stack. R0 ships only an empty importable skeleton so the workspace lint and test harness is green.
