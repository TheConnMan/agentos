# aci-protocol

Owning task: **C1**. The frozen ACI (Agent Container Interface) contract: the session protocol and NDJSON event types that every lane compiles against, authored as Pydantic models (source of truth) with committed JSON Schema export and generated TypeScript/Rust types. This package is a frozen interface: never change it unilaterally. A change stops the current task and escalates to the orchestrator (see repo `CLAUDE.md`). R0 ships only an empty importable skeleton so the workspace lint and test harness is green; C1 fills in the real contract.
