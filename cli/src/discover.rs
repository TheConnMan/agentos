//! Discover locally-authored plugin bundles under a directory (e.g. `agents/`),
//! for browsing outside the hardcoded `examples/` list.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

/// A plugin bundle found on disk: enough to display and to hand to
/// `skill up --plugin-dir`.
pub struct DiscoveredBundle {
    pub name: String,
    pub description: String,
    pub directory: PathBuf,
}

/// Scan `root`'s immediate subdirectories for a `.claude-plugin/plugin.json`
/// manifest, mirroring `scripts/check-plugin-compat.sh`'s depth-3 `find`.
///
/// Returns an empty Vec, not an error, when `root` does not exist -- an absent
/// scratch directory (nobody has made one yet) is the common case, not a
/// failure.
pub fn discover_bundles(root: &Path) -> Result<Vec<DiscoveredBundle>> {
    if !root.is_dir() {
        return Ok(Vec::new());
    }

    // Sorted for a deterministic listing, same rationale as bundle.rs's
    // archive walk.
    let mut entries: Vec<_> = std::fs::read_dir(root)
        .with_context(|| format!("reading {}", root.display()))?
        .collect::<std::io::Result<_>>()?;
    entries.sort_by_key(|e| e.file_name());

    let mut bundles = Vec::new();
    for entry in entries {
        let path = entry.path();
        if !path.is_dir() || !path.join(".claude-plugin/plugin.json").is_file() {
            continue;
        }
        let (manifest_path, manifest) = crate::scaffold::load_manifest_json(&path)?;
        let name = manifest
            .get("name")
            .and_then(|v| v.as_str())
            .with_context(|| format!("{} has no string 'name'", manifest_path.display()))?
            .to_string();
        let description = manifest
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        bundles.push(DiscoveredBundle {
            name,
            description,
            directory: path,
        });
    }
    Ok(bundles)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_when_the_directory_does_not_exist() {
        let dir = tempfile::tempdir().unwrap();
        let bundles = discover_bundles(&dir.path().join("nope")).unwrap();
        assert!(bundles.is_empty());
    }

    #[test]
    fn finds_a_valid_bundle() {
        let root = tempfile::tempdir().unwrap();
        crate::scaffold::scaffold(&root.path().join("deal-desk"), "deal-desk").unwrap();

        let bundles = discover_bundles(root.path()).unwrap();
        assert_eq!(bundles.len(), 1);
        assert_eq!(bundles[0].name, "deal-desk");
        assert_eq!(bundles[0].directory, root.path().join("deal-desk"));
    }

    #[test]
    fn skips_a_subdirectory_with_no_manifest() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("not-a-bundle")).unwrap();
        crate::scaffold::scaffold(&root.path().join("deal-desk"), "deal-desk").unwrap();

        let bundles = discover_bundles(root.path()).unwrap();
        assert_eq!(bundles.len(), 1);
        assert_eq!(bundles[0].name, "deal-desk");
    }

    #[test]
    fn sorts_deterministically_by_directory_name() {
        let root = tempfile::tempdir().unwrap();
        crate::scaffold::scaffold(&root.path().join("zeta"), "zeta").unwrap();
        crate::scaffold::scaffold(&root.path().join("alpha"), "alpha").unwrap();

        let bundles = discover_bundles(root.path()).unwrap();
        let names: Vec<_> = bundles.iter().map(|b| b.name.as_str()).collect();
        assert_eq!(names, vec!["alpha", "zeta"]);
    }
}
