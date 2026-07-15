//! OS-backed secret storage for local AgentOS workflows.
//!
//! Secret values live in the platform credential store via `keyring`. The only
//! filesystem state here is a non-secret index of names so `agentos secrets
//! list` can show what AgentOS has saved without needing credential-store
//! enumeration support.

use std::collections::BTreeSet;
use std::fs;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

const SERVICE: &str = "dev.curie.agentos";
const ACCOUNT_PREFIX: &str = "agentos:global:";

#[derive(Clone, Debug)]
pub struct SetSecretOpts {
    pub name: String,
    pub from_env: Option<String>,
}

#[derive(Clone, Debug)]
pub struct UnsetSecretOpts {
    pub name: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
struct SecretIndex {
    names: BTreeSet<String>,
}

pub fn set(opts: SetSecretOpts) -> Result<()> {
    validate_name(&opts.name)?;
    let value = match opts.from_env {
        Some(var) => std::env::var(&var)
            .with_context(|| format!("{var} is not set; cannot save {}", opts.name))?,
        None => prompt_secret(&opts.name)?,
    };
    save_value(&opts.name, &value)?;
    crate::ui::ui().success(&format!("saved {} in the OS credential store", opts.name));
    Ok(())
}

pub fn list() -> Result<()> {
    let names = list_names()?;
    let ui = crate::ui::ui();
    if ui.json() {
        ui.emit_json(&serde_json::json!({ "secrets": names }));
        return Ok(());
    }
    if names.is_empty() {
        ui.note("no AgentOS secrets saved");
    } else {
        ui.payload_plain(&names.join("\n"));
    }
    Ok(())
}

pub fn unset(opts: UnsetSecretOpts) -> Result<()> {
    remove_value(&opts.name)?;
    crate::ui::ui().success(&format!("removed {}", opts.name));
    Ok(())
}

pub fn get_value(name: &str) -> Result<Option<String>> {
    validate_name(name)?;
    let entry = entry(name)?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => {
            Err(err).with_context(|| format!("reading {name} from the OS credential store"))
        }
    }
}

/// Check the non-secret index without opening the platform credential store.
/// UI status rendering must use this instead of `get_value`.
pub fn is_saved(name: &str) -> Result<bool> {
    validate_name(name)?;
    Ok(read_index()?.names.contains(name))
}

pub fn set_value(name: &str, value: &str) -> Result<()> {
    validate_name(name)?;
    entry(name)?
        .set_password(value)
        .with_context(|| format!("saving {name} in the OS credential store"))
}

pub fn save_value(name: &str, value: &str) -> Result<()> {
    validate_name(name)?;
    if value.is_empty() {
        bail!("refusing to store an empty secret for {name}");
    }
    set_value(name, value)?;
    add_to_index(name)
}

pub fn delete_value(name: &str) -> Result<()> {
    validate_name(name)?;
    match entry(name)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => {
            Err(err).with_context(|| format!("removing {name} from the OS credential store"))
        }
    }
}

pub fn remove_value(name: &str) -> Result<()> {
    validate_name(name)?;
    delete_value(name)?;
    remove_from_index(name)
}

pub fn list_names() -> Result<Vec<String>> {
    let index = read_index()?;
    Ok(index.names.into_iter().collect())
}

pub fn validate_name(name: &str) -> Result<()> {
    if name.is_empty() {
        bail!("secret name is required");
    }
    let valid = name
        .chars()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_uppercase() || c == '_');
    if !valid {
        bail!(
            "secret name must look like an environment variable, e.g. GITHUB_PERSONAL_ACCESS_TOKEN"
        );
    }
    Ok(())
}

fn prompt_secret(name: &str) -> Result<String> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!("setting {name} requires a terminal; use --from-env VAR in non-interactive contexts");
    }
    print!("{name}: ");
    io::stdout().flush().ok();
    rpassword::read_password().context("reading secret from terminal")
}

fn entry(name: &str) -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, &account_name(name))
        .with_context(|| format!("opening OS credential-store entry for {name}"))
}

fn account_name(name: &str) -> String {
    format!("{ACCOUNT_PREFIX}{name}")
}

fn add_to_index(name: &str) -> Result<()> {
    let mut index = read_index()?;
    index.names.insert(name.to_string());
    write_index(&index)
}

fn remove_from_index(name: &str) -> Result<()> {
    let mut index = read_index()?;
    index.names.remove(name);
    write_index(&index)
}

fn read_index() -> Result<SecretIndex> {
    let path = index_path()?;
    if !path.is_file() {
        return Ok(SecretIndex::default());
    }
    let raw = fs::read_to_string(&path)
        .with_context(|| format!("reading secret index {}", path.display()))?;
    serde_json::from_str(&raw).with_context(|| format!("parsing secret index {}", path.display()))
}

fn write_index(index: &SecretIndex) -> Result<()> {
    let path = index_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating AgentOS config dir {}", parent.display()))?;
    }
    let body = serde_json::to_vec_pretty(index).context("serializing secret index")?;
    write_private(&path, &body)
}

#[cfg(unix)]
fn write_private(path: &Path, body: &[u8]) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;

    let mut file = fs::OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("writing secret index {}", path.display()))?;
    file.write_all(body)
        .with_context(|| format!("writing secret index {}", path.display()))
}

#[cfg(not(unix))]
fn write_private(path: &Path, body: &[u8]) -> Result<()> {
    fs::write(path, body).with_context(|| format!("writing secret index {}", path.display()))
}

fn index_path() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("AGENTOS_CONFIG_DIR") {
        return Ok(PathBuf::from(dir).join("secrets.json"));
    }
    let home =
        std::env::var("HOME").context("HOME is not set; cannot locate AgentOS config dir")?;
    Ok(PathBuf::from(home).join(".config/agentos/secrets.json"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_env_like_names() {
        assert!(validate_name("GITHUB_PERSONAL_ACCESS_TOKEN").is_ok());
        assert!(validate_name("_TOKEN1").is_ok());
        assert!(validate_name("github_token").is_err());
        assert!(validate_name("1TOKEN").is_err());
        assert!(validate_name("TOKEN-NOPE").is_err());
    }

    #[test]
    fn account_names_are_scoped_under_agentos() {
        assert_eq!(
            account_name("GITHUB_PERSONAL_ACCESS_TOKEN"),
            "agentos:global:GITHUB_PERSONAL_ACCESS_TOKEN"
        );
    }
}
