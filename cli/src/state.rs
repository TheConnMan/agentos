//! Local runner state: which container `agentos start` booted, and where.
//!
//! Written to `.agentos/runner.json` in the project directory so `send`,
//! `eval`, `status`, and `stop` find the runner without flags. The file is
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
}
