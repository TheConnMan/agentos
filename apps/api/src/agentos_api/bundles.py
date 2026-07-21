"""Plugin bundle intake: detect archive format, extract safely, validate.

The upload path is: bytes -> detect zip/tar(.gz) -> extract into a temp dir
(guarding against path traversal) -> locate the bundle root -> validate via the
frozen ``plugin_format.validate_bundle``. Storage and DB wiring live in the
router; this module is pure intake logic.
"""

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path

from plugin_format import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    MANIFEST_LOCATIONS,
    UnsupportedArchive,
    ValidationResult,
    bundle_root,
    safe_extract,
    validate_bundle,
)

# Re-exported so existing catchers (gitflow.py, routers/bundles.py, tests) keep
# resolving ``bundles.UnsupportedArchive`` after the extraction logic moved to
# plugin_format; safe_extract raises this single error for unsafe/unrecognized
# archives.
__all__ = [
    "UnsupportedArchive",
    "bundle_root",
    "detect_format",
    "extract_and_validate",
    "read_bundle_text_files",
]

# Detected format -> (stored key extension, content type). Detection sniffs the
# bytes rather than trusting the upload filename.
_ZIP = (".zip", "application/zip")
_TAR_GZ = (".tar.gz", "application/gzip")
_TAR = (".tar", "application/x-tar")


def detect_format(data: bytes) -> tuple[str, str]:
    """Return (extension, content_type) for the archive, sniffing the bytes."""

    if zipfile.is_zipfile(io.BytesIO(data)):
        return _ZIP
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"):
            return _TAR_GZ
    except tarfile.TarError:
        pass
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:"):
            return _TAR
    except tarfile.TarError:
        pass
    raise UnsupportedArchive("upload is not a zip or tar(.gz) archive")


def extract_and_validate(
    data: bytes,
    dest: Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> tuple[str, str, ValidationResult]:
    """Detect, extract, and validate. Returns (extension, content_type, result).

    Extraction (with the traversal/symlink/special-file and size/ratio guards)
    and the single-wrapper-dir unwrap live in ``plugin_format``; this only adds
    the storage-key/content-type detection the upload path needs. The size/ratio
    caps default to ``plugin_format``'s generous fallbacks; ``deploy.py`` passes
    the operator-configured ``Settings`` values instead.
    """

    extension, content_type = detect_format(data)
    safe_extract(
        data,
        dest,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    result = validate_bundle(bundle_root(dest))
    return extension, content_type, result


def _collect_text_files(root: Path) -> list[tuple[str, str]]:
    """The bundle's known text files as (bundle-relative posix path, content).

    Deliberately an allowlist of the bundle's structured text surfaces -- the
    manifest, the skill docs, and the eval cases -- so binaries (and anything
    else) are skipped, not just filtered by a guessed encoding. Paths are
    relative to the bundle root and posix so the UI reads a stable shape.
    """

    candidates: list[Path] = []
    for fixed in (*MANIFEST_LOCATIONS, Path("evals/cases.json")):
        if (root / fixed).is_file():
            candidates.append(root / fixed)
    candidates.extend(p for p in root.glob("skills/**/SKILL.md") if p.is_file())

    files: list[tuple[str, str]] = []
    for path in candidates:
        files.append((path.relative_to(root).as_posix(), path.read_text("utf-8")))
    return sorted(files, key=lambda item: item[0])


def read_bundle_text_files(data: bytes) -> list[tuple[str, str]]:
    """Extract an archive's bytes and return its known text files.

    Mirrors the upload path (detect -> extract into a temp dir, guarding path
    traversal -> unwrap to the bundle root) but reads the text surfaces instead of
    validating. Returns (path, content) pairs; the caller shapes the response.
    """

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        safe_extract(data, dest)
        return _collect_text_files(bundle_root(dest))
