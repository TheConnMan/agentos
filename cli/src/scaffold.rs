//! `agentos init`: scaffold a Claude Code plugin bundle.
//!
//! The layout matches the frozen `plugin-format` package: a manifest at
//! `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md` with YAML
//! frontmatter, a root `.mcp.json`, plus a CLI-local `evals/cases.json` seed
//! (a suite object `{name, cases: [{id, input, grader}]}`, hand-mirroring the
//! frozen eval-case schema) for `agentos skill eval`. Names are kebab-case per
//! the validator.

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

fn skill_md(name: &str) -> String {
    format!(
        "---\nname: {name}\ndescription: Look up a location's weather forecast using a live web search. Invoke whenever the user asks about the weather, whether to expect rain, temperature, or what to wear or plan for outdoor activities.\nallowed-tools:\n  - WebSearch\n  - WebFetch\n---\n\n# Weather\n\n## When to run\nThe user asks about the weather, a forecast, temperature, rain or snow chances, or whether a day suits an outdoor plan.\n\n## How to answer\n1. Determine the location and day. If the user named them, use them. If not, ask one short question instead of guessing.\n2. Run a web search for the forecast, e.g. `<city> weather forecast <day>`. Prefer a national weather service or a major forecast provider.\n3. Report, in two or three sentences: expected high and low, sky conditions, and precipitation chance. Include the location and day so there is no ambiguity.\n4. Name the source of the forecast at the end.\n\n## Hard rules\n\n- Never invent a forecast. If the search returns nothing usable, say so and name what you tried.\n- Temperatures in Fahrenheit first, Celsius in parentheses.\n- Keep the reply short enough to read in Slack without expanding.\n"
    )
}

fn eval_cases(name: &str) -> String {
    serde_json::to_string_pretty(&serde_json::json!({
        "name": name,
        "cases": [
            {
                "id": format!("{name}-answers"),
                "input": "What's the weather in San Francisco?",
                "grader": {
                    "kind": "contains",
                    "expected": "weather",
                    "case_sensitive": false,
                },
            }
        ],
    }))
    .expect("static eval cases serialize")
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
        assert_eq!(created.len(), 5);

        let manifest: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(dir.path().join(".claude-plugin/plugin.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(manifest["name"], "deal-desk");

        let skill = std::fs::read_to_string(dir.path().join("skills/deal-desk/SKILL.md")).unwrap();
        assert!(skill.starts_with("---\nname: deal-desk\n"));
        assert!(skill.contains("description: Look up a location's weather forecast"));
        assert!(skill.contains("allowed-tools:"));
        assert!(skill.contains("- WebSearch"));
        assert!(skill.contains("- WebFetch"));
        assert!(skill.contains("# Weather"));
        assert!(skill.contains("## How to answer"));
        assert!(skill.contains("Never invent a forecast"));

        let mcp: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(dir.path().join(".mcp.json")).unwrap())
                .unwrap();
        assert!(mcp["mcpServers"].is_object());

        let cases: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(dir.path().join("evals/cases.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(cases["name"], "deal-desk");
        let case_list = cases["cases"].as_array().unwrap();
        assert_eq!(case_list.len(), 1);
        assert_eq!(case_list[0]["id"], "deal-desk-answers");
        assert!(case_list[0]["input"].as_str().unwrap().contains("weather"));
        assert_eq!(case_list[0]["grader"]["kind"], "contains");
        assert_eq!(case_list[0]["grader"]["expected"], "weather");

        assert_eq!(
            read_manifest(dir.path()).unwrap(),
            ("deal-desk".to_string(), "0.1.0".to_string())
        );
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
    fn rejects_a_bad_name_before_touching_disk() {
        let dir = tempfile::tempdir().unwrap();
        assert!(scaffold(dir.path(), "Bad_Name").is_err());
        assert!(std::fs::read_dir(dir.path()).unwrap().next().is_none());
    }
}
