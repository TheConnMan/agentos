//! `agentos init --from-spec`: the agent-authored spec model + strict parse.
//!
//! A coding agent interviews the human and writes a JSON spec; the CLI scaffolds
//! a runnable bundle from it with zero interactive prompts (ADR-0021 decision 5).
//! Both structs use `deny_unknown_fields` so an authoring typo (e.g. `skils`)
//! fails loud rather than silently dropping the intended field -- the verify-first
//! ethos applied to the spec itself. The `evals` field reuses the frozen
//! `crate::evals::EvalCase` type directly, so a spec's eval shape is locked to the
//! same contract `agentos skill eval` loads and cannot drift.

use anyhow::{anyhow, bail, Result};
use serde::Deserialize;
use serde_json::{Map, Value};

use crate::evals::{validate_suite, EvalCase};
use crate::scaffold::valid_name;

/// One skill an agent describes: it becomes a `skills/<name>/SKILL.md`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SkillSpec {
    pub name: String,
    pub description: String,
    /// Verbatim Claude Code `allowed-tools` values; empty omits the key entirely.
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    pub instructions: String,
}

/// The whole bundle an agent describes. `connectors` is the raw `.mcp.json`
/// `mcpServers` map (server name -> server object) and is written through
/// verbatim after validation.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSpec {
    pub name: String,
    pub description: String,
    pub skills: Vec<SkillSpec>,
    #[serde(default)]
    pub connectors: Map<String, Value>,
    pub evals: Vec<EvalCase>,
}

/// Deserialize the spec JSON (strict) then validate it. Returns actionable
/// errors that name the offending value so an agent can self-correct.
pub fn parse(json: &str) -> Result<AgentSpec> {
    // Inline serde's message (not just wrap it) so `deny_unknown_fields` errors
    // that name the offending field -- e.g. "unknown field `skils`" -- survive an
    // anyhow `.to_string()`, which only renders the outermost context otherwise.
    let spec: AgentSpec = serde_json::from_str(json)
        .map_err(|err| anyhow!("spec is not a valid agent spec (JSON): {err}"))?;
    validate(&spec)?;
    Ok(spec)
}

fn validate(spec: &AgentSpec) -> Result<()> {
    if !valid_name(&spec.name) {
        bail!(
            "spec name {:?} must be kebab-case (lowercase letters, digits, hyphens)",
            spec.name
        );
    }
    if spec.skills.is_empty() {
        bail!("spec must define at least one skill");
    }

    // Skill names must be valid kebab and unique: they collide on the same
    // `skills/<name>/SKILL.md` path otherwise, which we refuse before any write.
    let mut seen = std::collections::HashSet::new();
    for skill in &spec.skills {
        if !valid_name(&skill.name) {
            bail!(
                "spec skill name {:?} must be kebab-case (lowercase letters, digits, hyphens)",
                skill.name
            );
        }
        if !seen.insert(skill.name.as_str()) {
            bail!(
                "spec has two skills named {:?}; skill names must be unique",
                skill.name
            );
        }
    }

    if spec.evals.is_empty() {
        bail!("spec must define at least one eval case");
    }

    // Mirror plugin_format `_validate_mcp_object`: a connector object is only
    // usable if it defines `command` (stdio) or `url` (remote); reject anything
    // else rather than scaffold a broken `.mcp.json`. When a field is present it
    // must be a STRING: the frozen `McpServer` types `command`/`url` as `str`, so
    // a non-string here (e.g. `{"command": 42}`) would pass this gate but make
    // `validate_bundle` reject the emitted `.mcp.json` -- catch it now with a
    // message that names the offending field.
    for (name, value) in &spec.connectors {
        let obj = value.as_object();
        for key in ["command", "url"] {
            if let Some(field) = obj.and_then(|o| o.get(key)) {
                if !field.is_null() && !field.is_string() {
                    bail!("connector {name:?} field '{key}' must be a string");
                }
            }
        }
        let defines = |key: &str| obj.and_then(|o| o.get(key)).is_some_and(|v| v.is_string());
        if !(defines("command") || defines("url")) {
            bail!("connector {name:?} must define either 'command' (stdio) or 'url' (remote)");
        }
    }

    // Reuse the frozen suite-level discipline (empty-cases + regex compile) so a
    // spec's evals are held to the exact contract `agentos skill eval` enforces.
    // Borrow the spec's own name/evals directly rather than cloning them into a
    // throwaway suite purely to validate.
    validate_suite(&spec.name, &spec.evals)?;

    Ok(())
}
