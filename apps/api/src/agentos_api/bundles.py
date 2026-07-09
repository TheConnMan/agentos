"""Plugin bundle intake: detect archive format, extract safely, validate.

The upload path is: bytes -> detect zip/tar(.gz) -> extract into a temp dir
(guarding against path traversal) -> locate the bundle root -> validate via the
frozen ``plugin_format.validate_bundle``. Storage and DB wiring live in the
router; this module is pure intake logic.
"""

import io
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from plugin_format import ValidationResult, validate_bundle

# Detected format -> (stored key extension, content type). Detection sniffs the
# bytes rather than trusting the upload filename.
_ZIP = (".zip", "application/zip")
_TAR_GZ = (".tar.gz", "application/gzip")
_TAR = (".tar", "application/x-tar")


class UnsupportedArchive(Exception):
    """The upload is not a recognized zip or tar(.gz) archive."""


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


def extract(data: bytes, extension: str, dest: Path) -> None:
    """Extract the archive into ``dest``, refusing entries that escape it."""

    if extension == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # zip has no built-in equivalent of tar's filter="data", so guard
            # traversal and symlink entries by hand: a symlink like
            # `x -> ../../etc/passwd` passes a name-only `..` check yet escapes
            # `dest` when followed (the tar path below is covered by filter="data").
            for info in zf.infolist():
                name = info.filename
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise UnsupportedArchive(f"unsafe path in archive: {name}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise UnsupportedArchive(f"symlink entry not allowed in archive: {name}")
            zf.extractall(dest)
        return
    mode: Literal["r:gz", "r:"] = "r:gz" if extension == ".tar.gz" else "r:"
    with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
        try:
            # filter="data" (py3.12+) blocks absolute paths, traversal, symlinks,
            # and special files; surface a rejection as UnsupportedArchive so the
            # upload route answers 4xx rather than 500.
            tf.extractall(dest, filter="data")
        except tarfile.FilterError as exc:
            raise UnsupportedArchive(f"unsafe entry in archive: {exc}") from exc


# Where the frozen validator looks for the manifest; used here only to locate
# the bundle root inside an archive that wraps everything in one folder.
_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


def _has_manifest(directory: Path) -> bool:
    return any((directory / loc).is_file() for loc in _MANIFEST_LOCATIONS)


def bundle_root(extracted: Path) -> Path:
    """The directory to validate: unwrap a single top-level folder if present.

    A manifest at the extraction root means the archive was made flat; otherwise
    a common `tar czf bundle.tgz myplugin/` wraps everything in one directory, so
    descend into it when that single subdir carries the manifest.
    """

    if _has_manifest(extracted):
        return extracted
    subdirs = [p for p in extracted.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and _has_manifest(subdirs[0]):
        return subdirs[0]
    return extracted


def extract_and_validate(data: bytes, dest: Path) -> tuple[str, str, ValidationResult]:
    """Detect, extract, and validate. Returns (extension, content_type, result)."""

    extension, content_type = detect_format(data)
    extract(data, extension, dest)
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
    for fixed in (*_MANIFEST_LOCATIONS, Path("evals/cases.json")):
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
        extension, _ = detect_format(data)
        extract(data, extension, dest)
        return _collect_text_files(bundle_root(dest))
