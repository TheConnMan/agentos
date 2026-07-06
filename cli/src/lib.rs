//! AgentOS CLI library: everything behind the `agentos` binary.
//!
//! The CLI speaks only the frozen contracts: ACI frames over HTTP/NDJSON to a
//! local runner container (via the generated `agentos-aci-protocol` crate) and
//! the platform API's committed OpenAPI surface. Task I1.

pub mod api;
pub mod bundle;
pub mod chat;
pub mod commands;
pub mod docker;
pub mod evals;
pub mod local;
pub mod message;
pub mod ndjson;
pub mod ops;
pub mod queue;
pub mod render;
pub mod runner;
pub mod scaffold;
pub mod state;
