//! Package a plugin bundle directory into a tar.gz archive for upload.
//!
//! The platform API (B2) accepts tar.gz/tar/zip, extracts, and validates via
//! the frozen plugin-format package. Workstation state (`.agentos/`, `.git/`)
//! never belongs in an immutable bundle, so it is excluded here.

use std::path::Path;

use anyhow::{bail, Context, Result};
use flate2::write::GzEncoder;
use flate2::Compression;

const EXCLUDED_DIRS: &[&str] = &[".agentos", ".git"];

fn append_dir(
    builder: &mut tar::Builder<GzEncoder<Vec<u8>>>,
    root: &Path,
    dir: &Path,
) -> Result<()> {
    // Sorted for deterministic archives: same tree, same byte layout order.
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .with_context(|| format!("reading {}", dir.display()))?
        .collect::<std::io::Result<_>>()?;
    entries.sort_by_key(|e| e.file_name());

    for entry in entries {
        let path = entry.path();
        let name = entry.file_name();
        let rel = path.strip_prefix(root).expect("entry is under root");
        // file_type() does not follow symlinks: a link inside the bundle would
        // otherwise be dereferenced by tar and upload host files from outside
        // the plugin root (e.g. a link into ~/.ssh). Refuse loudly instead.
        let file_type = entry
            .file_type()
            .with_context(|| format!("stat {}", path.display()))?;
        if file_type.is_symlink() {
            bail!(
                "symlinks are not supported in plugin bundles: {}",
                path.display()
            );
        }
        if file_type.is_dir() {
            if EXCLUDED_DIRS.iter().any(|d| name == *d) {
                continue;
            }
            append_dir(builder, root, &path)?;
        } else if file_type.is_file() {
            builder
                .append_path_with_name(&path, rel)
                .with_context(|| format!("archiving {}", path.display()))?;
        }
    }
    Ok(())
}

/// Tar-gzip the bundle at `dir`, returning the archive bytes.
pub fn pack_tar_gz(dir: &Path) -> Result<Vec<u8>> {
    if !dir.is_dir() {
        bail!("bundle path is not a directory: {}", dir.display());
    }
    let encoder = GzEncoder::new(Vec::new(), Compression::default());
    let mut builder = tar::Builder::new(encoder);
    append_dir(&mut builder, dir, dir)?;
    let encoder = builder.into_inner().context("finalizing tar archive")?;
    encoder.finish().context("finalizing gzip stream")
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::read::GzDecoder;

    fn entry_names(bytes: &[u8]) -> Vec<String> {
        let mut archive = tar::Archive::new(GzDecoder::new(bytes));
        archive
            .entries()
            .unwrap()
            .map(|e| e.unwrap().path().unwrap().to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn packs_the_bundle_and_excludes_workstation_state() {
        let dir = tempfile::tempdir().unwrap();
        crate::scaffold::scaffold(dir.path(), "deal-desk").unwrap();
        std::fs::create_dir_all(dir.path().join(".agentos")).unwrap();
        std::fs::write(dir.path().join(".agentos/runner.json"), "{}").unwrap();
        std::fs::create_dir_all(dir.path().join(".git")).unwrap();
        std::fs::write(dir.path().join(".git/HEAD"), "ref").unwrap();

        let names = entry_names(&pack_tar_gz(dir.path()).unwrap());
        assert!(names.contains(&".claude-plugin/plugin.json".to_string()));
        assert!(names.contains(&"skills/deal-desk/SKILL.md".to_string()));
        assert!(names.contains(&".mcp.json".to_string()));
        assert!(!names.iter().any(|n| n.starts_with(".agentos")));
        assert!(!names.iter().any(|n| n.starts_with(".git/")));
    }

    #[test]
    fn refuses_a_missing_directory() {
        assert!(pack_tar_gz(Path::new("/nonexistent/bundle")).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn refuses_symlinks_instead_of_dereferencing_them() {
        let dir = tempfile::tempdir().unwrap();
        crate::scaffold::scaffold(dir.path(), "deal-desk").unwrap();
        let secret = tempfile::tempdir().unwrap();
        std::fs::write(secret.path().join("id_rsa"), "private").unwrap();
        std::os::unix::fs::symlink(secret.path(), dir.path().join("skills/data")).unwrap();

        let err = pack_tar_gz(dir.path()).unwrap_err();
        assert!(
            err.to_string().contains("symlinks are not supported"),
            "{err}"
        );
    }
}
