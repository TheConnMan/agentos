//! Local runner state: which container `agentos skill up` booted, and where.
//!
//! Written to `.agentos/runner.json` in the project directory so `message`,
//! `eval`, `cluster status`, and `cluster down` find the runner without flags. The file is
//! local workstation state, never committed (init scaffolds the ignore rule).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

pub const STATE_DIR: &str = ".agentos";
pub const STATE_FILE: &str = "runner.json";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunnerState {
    pub container_id: String,
    pub container_name: String,
    pub image: String,
    pub port: u16,
    pub base_url: String,
    pub session_id: String,
    pub plugin_dir: String,
    pub fake_model: bool,
    #[serde(default)]
    pub ollama_container: Option<String>,
    #[serde(default)]
    pub network: Option<String>,
    #[serde(default)]
    pub model_base_url: Option<String>,
}

fn state_path(dir: &Path) -> PathBuf {
    dir.join(STATE_DIR).join(STATE_FILE)
}

pub fn save(dir: &Path, state: &RunnerState) -> Result<()> {
    let path = state_path(dir);
    std::fs::create_dir_all(path.parent().expect("state path has a parent"))?;
    let body = serde_json::to_string_pretty(state)?;
    std::fs::write(&path, body).with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

pub fn load(dir: &Path) -> Result<Option<RunnerState>> {
    let path = state_path(dir);
    if !path.is_file() {
        return Ok(None);
    }
    let body =
        std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    let state = serde_json::from_str(&body)
        .with_context(|| format!("{} is not a valid runner state file", path.display()))?;
    Ok(Some(state))
}

pub fn remove(dir: &Path) -> Result<()> {
    let path = state_path(dir);
    if path.is_file() {
        std::fs::remove_file(&path).with_context(|| format!("removing {}", path.display()))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> RunnerState {
        RunnerState {
            container_id: "abc123".into(),
            container_name: "agentos-runner-local".into(),
            image: "agentos-runner".into(),
            port: 7245,
            base_url: "http://localhost:7245".into(),
            session_id: "local-1".into(),
            plugin_dir: "/tmp/deal-desk".into(),
            fake_model: true,
            ollama_container: None,
            network: None,
            model_base_url: None,
        }
    }

    #[test]
    fn round_trips_through_the_state_file() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load(dir.path()).unwrap(), None);
        save(dir.path(), &sample()).unwrap();
        assert_eq!(load(dir.path()).unwrap(), Some(sample()));
        remove(dir.path()).unwrap();
        assert_eq!(load(dir.path()).unwrap(), None);
    }

    #[test]
    fn corrupt_state_is_an_error_not_a_silent_none() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join(STATE_DIR)).unwrap();
        std::fs::write(dir.path().join(STATE_DIR).join(STATE_FILE), "not json").unwrap();
        assert!(load(dir.path()).is_err());
    }

    #[test]
    fn round_trip_preserves_local_model_runner_fields() {
        let dir = tempfile::tempdir().unwrap();
        let state = RunnerState {
            ollama_container: Some("agentos-ollama".into()),
            network: Some("agentos_default".into()),
            model_base_url: Some("http://ollama:11434".into()),
            ..sample()
        };
        save(dir.path(), &state).unwrap();
        assert_eq!(load(dir.path()).unwrap(), Some(state));
    }

    #[test]
    fn load_accepts_older_state_without_local_model_keys() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join(STATE_DIR)).unwrap();
        std::fs::write(
            dir.path().join(STATE_DIR).join(STATE_FILE),
            r#"{
  "container_id": "abc123",
  "container_name": "agentos-runner-local",
  "image": "agentos-runner",
  "port": 7245,
  "base_url": "http://localhost:7245",
  "session_id": "local-1",
  "plugin_dir": "/tmp/deal-desk",
  "fake_model": true
}"#,
        )
        .unwrap();
        let loaded = load(dir.path()).unwrap();
        assert!(loaded.is_some());
        assert_eq!(loaded.unwrap().ollama_container, None);
    }
}
