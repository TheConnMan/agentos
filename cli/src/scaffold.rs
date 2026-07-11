//! `agentos init`: scaffold a Claude Code plugin bundle.
//!
//! Seven files. The deployed bundle shape matches the frozen `plugin-format`
//! package: a manifest at `.claude-plugin/plugin.json`, a genericized starter
//! `skills/<name>/SKILL.md` with YAML frontmatter, a root `.mcp.json`, plus a
//! CLI-local `evals/cases.json` seed (a suite object
//! `{name, cases: [{id, input, grader}]}`, hand-mirroring the frozen eval-case
//! schema) for `agentos skill eval` -- a single smoke case that passes on any
//! completed turn. Two more files teach the developer's coding agent how to
//! drive the harness: a root `AGENTS.md` (the cross-agent auto-scanned standard)
//! and an installable primer skill at `.claude/skills/using-agentos/SKILL.md`
//! whose body is rendered from `guide::primer_markdown()` so it can never drift
//! from `agentos guide`. Names are kebab-case per the validator.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

/// Kebab-case check mirroring plugin_format's `^[a-z0-9]+(-[a-z0-9]+)*$`.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && !name.starts_with('-')
        && !name.ends_with('-')
        && !name.contains("--")
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn manifest(name: &str) -> String {
    serde_json::to_string_pretty(&serde_json::json!({
        "name": name,
        "description": format!("The {name} agent plugin."),
        "version": "0.1.0",
    }))
    .expect("static manifest serializes")
}

/// The genericized starter skill for `<name>`: a believable, editable
/// placeholder scoped to the bundle name (no weather demo). Keeps the
/// correct-by-example `allowed-tools` key and the when/how/rules section shape
/// a real skill uses.
fn skill_md(name: &str) -> String {
    format!(
        "---\nname: {name}\ndescription: Starter skill for the {name} agent. Replace this description with when the agent should invoke this skill -- it is the routing signal.\nallowed-tools:\n  - WebSearch\n  - WebFetch\n---\n\n# {name}\n\nThis is the starter skill scaffolded by `agentos init`. Replace each section\nbelow with your agent's real behavior; keep the section shape.\n\n## When to run\nDescribe the requests this skill should handle.\n\n## How to answer\n1. Numbered, concrete steps the agent follows.\n2. Prefer verifiable sources and tools over recall.\n\n## Hard rules\n- Never invent an answer. If you cannot find one, say so and name what you tried.\n- Keep replies short enough to read in Slack without expanding.\n"
    )
}

/// The `evals/cases.json` seed: a single smoke case named for `<name>`. The
/// `contains ""` grader passes on any completed (`Done`) turn, so the scaffold
/// is green out of the box under both `--fake-model` and a real credential --
/// a status gate the author replaces with real graders first.
fn eval_cases(name: &str) -> String {
    serde_json::to_string_pretty(&serde_json::json!({
        "name": name,
        "cases": [
            {
                "id": format!("{name}-smoke"),
                "input": format!("In one short sentence, introduce yourself as the {name} agent."),
                "grader": {
                    "kind": "contains",
                    "expected": "",
                    "case_sensitive": false,
                },
            }
        ],
    }))
    .expect("static eval cases serialize")
}

/// The root `AGENTS.md`: the cross-agent auto-scanned standard carrying the
/// non-discoverable operating rules (the authoring loop, eval-as-promotion-gate,
/// verify-first, the top landmines) and a pointer to `agentos guide` for the
/// full primer.
fn agents_md(name: &str) -> String {
    format!(
        "# Agent instructions: {name}\n\nThis is an AgentOS bundle (a Claude Code plugin shape). The full harness\nprimer is one command away and is the source of truth:\n\n    agentos guide\n\n## The loop\n\n1. `agentos skill up --fake-model` -- boot the runner offline, no credential.\n2. Edit `skills/{name}/SKILL.md` (behavior) and `evals/cases.json` (the contract).\n3. `agentos skill eval` -- must be green before any deploy. Merging to main promotes.\n4. `agentos skill down` when finished.\n\n## Rules\n\n- Verify before running: `agentos schema` lists every real command; never\n  invoke one you have not confirmed.\n- The eval file is the promotion gate and never changes across tiers\n  (skill/local/cluster). Never deploy on red.\n- Landmines: run `agentos guide` (or read\n  `.claude/skills/using-agentos/SKILL.md`) for the full, current list. The most\n  common: skill frontmatter uses `allowed-tools`, not `tools` (the wrong key\n  parses but silently grants no tools).\n- The scaffolded eval is a smoke test (passes on any completed turn).\n  Replace it with real graders as the first authoring step.\n"
    )
}

/// The installable harness primer skill at `.claude/skills/using-agentos/`,
/// auto-discovered by Claude Code as a PROJECT skill. Frontmatter (guidance-only,
/// so no `allowed-tools`) followed by the guide body VERBATIM from
/// `crate::guide::primer_markdown()` -- one source of truth, drift-gated.
fn using_agentos_skill() -> String {
    format!(
        "---\nname: using-agentos\ndescription: How to drive the AgentOS harness -- the parity ladder, tier decision logic, landmines, and recovery steps. Invoke when running agentos commands, authoring or evaluating a bundle, or debugging a divergence between skill, local, and cluster tiers.\n---\n\n{}",
        crate::guide::primer_markdown()
    )
}

const MCP_JSON: &str = "{\n  \"mcpServers\": {}\n}\n";
const GITIGNORE: &str = ".agentos/\n";

/// Create the bundle skeleton under `dir`; returns the created files.
pub fn scaffold(dir: &Path, name: &str) -> Result<Vec<PathBuf>> {
    if !valid_name(name) {
        bail!("plugin name {name:?} must be kebab-case (lowercase letters, digits, hyphens)");
    }

    let files: Vec<(PathBuf, String)> = vec![
        (dir.join(".claude-plugin/plugin.json"), manifest(name)),
        (dir.join(format!("skills/{name}/SKILL.md")), skill_md(name)),
        (dir.join(".mcp.json"), MCP_JSON.to_string()),
        (dir.join("evals/cases.json"), eval_cases(name)),
        (dir.join(".gitignore"), GITIGNORE.to_string()),
        (dir.join("AGENTS.md"), agents_md(name)),
        (
            dir.join(".claude/skills/using-agentos/SKILL.md"),
            using_agentos_skill(),
        ),
    ];

    // Refuse if ANY target (or a stray manifest) already exists: init must
    // never truncate a file the user already has (e.g. a real .mcp.json).
    let mut collisions: Vec<PathBuf> = files
        .iter()
        .map(|(path, _)| path.clone())
        .filter(|path| path.exists())
        .collect();
    if dir.join("plugin.json").exists() {
        collisions.push(dir.join("plugin.json"));
    }
    if !collisions.is_empty() {
        let listed: Vec<String> = collisions.iter().map(|p| p.display().to_string()).collect();
        bail!(
            "refusing to overwrite existing files: {}",
            listed.join(", ")
        );
    }

    let mut created = Vec::new();
    for (path, body) in files {
        let parent = path.parent().expect("scaffold paths have parents");
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating {}", parent.display()))?;
        std::fs::write(&path, body).with_context(|| format!("writing {}", path.display()))?;
        created.push(path);
    }
    Ok(created)
}

/// Read the plugin name and version from a bundle's manifest.
pub fn read_manifest(dir: &Path) -> Result<(String, String)> {
    let path = [".claude-plugin/plugin.json", "plugin.json"]
        .iter()
        .map(|rel| dir.join(rel))
        .find(|p| p.is_file())
        .with_context(|| format!("no plugin manifest under {}", dir.display()))?;
    let body =
        std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    let value: serde_json::Value = serde_json::from_str(&body)
        .with_context(|| format!("{} is not valid JSON", path.display()))?;
    let name = value
        .get("name")
        .and_then(|v| v.as_str())
        .with_context(|| format!("{} has no string 'name'", path.display()))?
        .to_string();
    let version = value
        .get("version")
        .and_then(|v| v.as_str())
        .unwrap_or("0.0.0")
        .to_string();
    Ok((name, version))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_kebab_case_and_rejects_everything_else() {
        for good in ["deal-desk", "a", "x9", "a-b-c1"] {
            assert!(valid_name(good), "{good} should be valid");
        }
        for bad in ["", "Deal-Desk", "deal_desk", "-x", "x-", "a--b", "a b"] {
            assert!(!valid_name(bad), "{bad} should be invalid");
        }
    }

    #[test]
    fn scaffolds_the_frozen_bundle_shape() {
        let dir = tempfile::tempdir().unwrap();
        let created = scaffold(dir.path(), "deal-desk").unwrap();
        assert_eq!(created.len(), 7);

        let manifest: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(dir.path().join(".claude-plugin/plugin.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(manifest["name"], "deal-desk");

        // Genericized starter skill for <name>: no weather anywhere.
        let skill = std::fs::read_to_string(dir.path().join("skills/deal-desk/SKILL.md")).unwrap();
        assert!(skill.starts_with("---\nname: deal-desk\n"));
        let description_line = skill
            .lines()
            .find(|l| l.starts_with("description:"))
            .expect("skill has a description line");
        assert!(
            description_line.contains("deal-desk"),
            "description mentions the name: {description_line}"
        );
        assert!(skill.contains("allowed-tools:"));
        assert!(
            !skill.contains("weather") && !skill.contains("Weather"),
            "starter skill must be genericized, not weather"
        );

        let mcp: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(dir.path().join(".mcp.json")).unwrap())
                .unwrap();
        assert!(mcp["mcpServers"].is_object());

        // Single smoke case named for <name>: contains "" (status-gated pass).
        let cases: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(dir.path().join("evals/cases.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(cases["name"], "deal-desk");
        let case_list = cases["cases"].as_array().unwrap();
        assert_eq!(case_list.len(), 1);
        assert_eq!(case_list[0]["id"], "deal-desk-smoke");
        assert_eq!(case_list[0]["grader"]["kind"], "contains");
        assert_eq!(case_list[0]["grader"]["expected"], "");
        assert_eq!(case_list[0]["grader"]["case_sensitive"], false);

        // AGENTS.md teaches the developer's coding agent the harness.
        let agents = std::fs::read_to_string(dir.path().join("AGENTS.md")).unwrap();
        assert!(agents.contains("agentos guide"));
        assert!(agents.contains("agentos skill eval"));
        assert!(agents.contains("allowed-tools"));

        // The installable harness primer skill, discovered by Claude Code.
        let harness_skill =
            std::fs::read_to_string(dir.path().join(".claude/skills/using-agentos/SKILL.md"))
                .unwrap();
        assert!(harness_skill.starts_with("---\nname: using-agentos\n"));
        let harness_description = harness_skill
            .lines()
            .find(|l| l.starts_with("description:"))
            .expect("harness skill has a description line");
        assert!(
            !harness_description
                .trim_start_matches("description:")
                .trim()
                .is_empty(),
            "harness skill description is non-empty"
        );
        assert!(harness_skill.contains("# AgentOS harness primer"));
        assert!(harness_skill.contains("agentos schema"));

        assert_eq!(
            read_manifest(dir.path()).unwrap(),
            ("deal-desk".to_string(), "0.1.0".to_string())
        );
    }

    #[test]
    fn harness_skill_body_equals_the_guide() {
        // Anti-drift (D2): the scaffolded harness skill body is rendered from
        // `guide::primer_markdown()`, never re-authored, so the guide and the
        // scaffolded skill can never diverge. The body after the frontmatter's
        // closing `---\n` (plus its blank line) must equal the guide verbatim.
        let dir = tempfile::tempdir().unwrap();
        scaffold(dir.path(), "deal-desk").unwrap();
        let content =
            std::fs::read_to_string(dir.path().join(".claude/skills/using-agentos/SKILL.md"))
                .unwrap();
        let body = content
            .splitn(3, "---\n")
            .nth(2)
            .expect("frontmatter-delimited body")
            .strip_prefix('\n')
            .expect("blank line after the closing frontmatter fence");
        assert_eq!(body, crate::guide::primer_markdown());
    }

    #[test]
    fn eval_cases_byte_match_the_committed_fixture() {
        // The frozen cross-language fixture: `eval_cases("example")` must be
        // byte-for-byte the worker's committed example, so a scaffolded bundle
        // is loadable by the platform eval path.
        assert_eq!(
            eval_cases("example"),
            include_str!("../../apps/worker/schema/eval-cases.example.json")
        );
    }

    #[test]
    fn refuses_to_overwrite_an_existing_bundle() {
        let dir = tempfile::tempdir().unwrap();
        scaffold(dir.path(), "deal-desk").unwrap();
        assert!(scaffold(dir.path(), "deal-desk").is_err());
    }

    #[test]
    fn refuses_to_truncate_any_existing_target_even_without_a_manifest() {
        let dir = tempfile::tempdir().unwrap();
        let existing = r#"{"mcpServers":{"important":{"command":"x"}}}"#;
        std::fs::write(dir.path().join(".mcp.json"), existing).unwrap();

        let err = scaffold(dir.path(), "deal-desk").unwrap_err();
        assert!(err.to_string().contains(".mcp.json"), "{err}");
        assert_eq!(
            std::fs::read_to_string(dir.path().join(".mcp.json")).unwrap(),
            existing,
            "existing file must be untouched"
        );
        assert!(!dir.path().join(".claude-plugin/plugin.json").exists());
    }

    #[test]
    fn refuses_to_overwrite_an_existing_agents_md() {
        let dir = tempfile::tempdir().unwrap();
        let existing = "# My own agent notes\nDo not clobber me.\n";
        std::fs::write(dir.path().join("AGENTS.md"), existing).unwrap();

        let err = scaffold(dir.path(), "deal-desk").unwrap_err();
        assert!(err.to_string().contains("AGENTS.md"), "{err}");
        assert_eq!(
            std::fs::read_to_string(dir.path().join("AGENTS.md")).unwrap(),
            existing,
            "existing file must be untouched"
        );
        assert!(!dir.path().join(".claude-plugin/plugin.json").exists());
    }

    #[test]
    fn rejects_a_bad_name_before_touching_disk() {
        let dir = tempfile::tempdir().unwrap();
        assert!(scaffold(dir.path(), "Bad_Name").is_err());
        assert!(std::fs::read_dir(dir.path()).unwrap().next().is_none());
    }
}
