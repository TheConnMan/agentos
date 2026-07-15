//! OS-backed secret storage for local AgentOS workflows.
//!
//! Secret values live together in one platform credential-store vault via
//! `keyring`, so a workflow authorizes AgentOS once rather than once per key.
//! The only filesystem state here is a non-secret index of names so `agentos
//! secrets list` can show what AgentOS has saved without opening the vault.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

const SERVICE: &str = "dev.curie.agentos";
const ACCOUNT_PREFIX: &str = "agentos:global:";
const VAULT_ACCOUNT: &str = "agentos:global:vault";

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

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
struct SecretVault {
    values: BTreeMap<String, String>,
}

#[derive(Default)]
struct VaultCache {
    loaded: bool,
    vault: SecretVault,
}

static VAULT_CACHE: OnceLock<Mutex<VaultCache>> = OnceLock::new();

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
    let mut vault = read_vault()?;
    if let Some(value) = vault.values.get(name) {
        return Ok(Some(value.clone()));
    }

    // Migrate legacy per-key entries lazily. Existing users authorize each old
    // item at most once; all subsequent reads come from the single vault item.
    if let Some(value) = read_legacy_value(name)? {
        vault.values.insert(name.to_string(), value.clone());
        write_vault(&vault)?;
        let _ = delete_legacy_value(name);
        return Ok(Some(value));
    }
    Ok(None)
}

/// Check the non-secret index without opening the platform credential store.
/// UI status rendering must use this instead of `get_value`.
pub fn is_saved(name: &str) -> Result<bool> {
    validate_name(name)?;
    Ok(read_index()?.names.contains(name))
}

pub fn set_value(name: &str, value: &str) -> Result<()> {
    validate_name(name)?;
    let mut vault = read_vault()?;
    vault.values.insert(name.to_string(), value.to_string());
    write_vault(&vault)
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
    let mut vault = read_vault()?;
    if vault.values.remove(name).is_some() {
        if vault.values.is_empty() {
            delete_vault()?;
        } else {
            write_vault(&vault)?;
        }
        return Ok(());
    }
    delete_legacy_value(name)
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

fn vault_entry() -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, VAULT_ACCOUNT)
        .context("opening the AgentOS OS credential-store vault")
}

fn legacy_entry(name: &str) -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, &account_name(name))
        .with_context(|| format!("opening OS credential-store entry for {name}"))
}

fn vault_cache() -> &'static Mutex<VaultCache> {
    VAULT_CACHE.get_or_init(|| Mutex::new(VaultCache::default()))
}

fn read_vault() -> Result<SecretVault> {
    let mut cache = vault_cache()
        .lock()
        .map_err(|_| anyhow::anyhow!("AgentOS credential vault cache is unavailable"))?;
    if cache.loaded {
        return Ok(cache.vault.clone());
    }
    let vault = match vault_entry()?.get_password() {
        Ok(raw) => serde_json::from_str(&raw).context("parsing the AgentOS credential vault")?,
        Err(keyring::Error::NoEntry) => SecretVault::default(),
        Err(err) => return Err(err).context("reading the AgentOS OS credential-store vault"),
    };
    cache.loaded = true;
    cache.vault = vault.clone();
    Ok(vault)
}

fn write_vault(vault: &SecretVault) -> Result<()> {
    let raw = serde_json::to_string(vault).context("serializing the AgentOS credential vault")?;
    vault_entry()?
        .set_password(&raw)
        .context("saving the AgentOS OS credential-store vault")?;
    let mut cache = vault_cache()
        .lock()
        .map_err(|_| anyhow::anyhow!("AgentOS credential vault cache is unavailable"))?;
    cache.loaded = true;
    cache.vault = vault.clone();
    Ok(())
}

fn delete_vault() -> Result<()> {
    match vault_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => {}
        Err(err) => return Err(err).context("removing the AgentOS credential-store vault"),
    }
    let mut cache = vault_cache()
        .lock()
        .map_err(|_| anyhow::anyhow!("AgentOS credential vault cache is unavailable"))?;
    cache.loaded = true;
    cache.vault = SecretVault::default();
    Ok(())
}

fn read_legacy_value(name: &str) -> Result<Option<String>> {
    match legacy_entry(name)?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => {
            Err(err).with_context(|| format!("reading {name} from the OS credential store"))
        }
    }
}

fn delete_legacy_value(name: &str) -> Result<()> {
    match legacy_entry(name)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => {
            Err(err).with_context(|| format!("removing legacy {name} credential-store entry"))
        }
    }
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

    #[test]
    fn vault_round_trips_multiple_credentials_in_one_payload() {
        let vault = SecretVault {
            values: BTreeMap::from([
                ("ANTHROPIC_API_KEY".to_string(), "model-secret".to_string()),
                (
                    "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
                    "github-secret".to_string(),
                ),
            ]),
        };
        let raw = serde_json::to_string(&vault).unwrap();
        let decoded: SecretVault = serde_json::from_str(&raw).unwrap();

        assert_eq!(decoded.values, vault.values);
        assert_eq!(VAULT_ACCOUNT, "agentos:global:vault");
    }
}
