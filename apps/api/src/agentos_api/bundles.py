"""Plugin bundle intake: detect archive format, extract safely, validate.

The upload path is: bytes -> detect zip/tar(.gz) -> extract into a temp dir
(guarding against path traversal) -> locate the bundle root -> validate via the
frozen ``plugin_format.validate_bundle``. Storage and DB wiring live in the
router; this module is pure intake logic.
"""

import io
import tarfile
import zipfile
from pathlib import Path

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
            for name in zf.namelist():
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise UnsupportedArchive(f"unsafe path in archive: {name}")
            zf.extractall(dest)
    elif extension == ".tar.gz":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            # filter="data" (py3.12+) blocks absolute paths, traversal, and
            # special files.
            tf.extractall(dest, filter="data")
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            tf.extractall(dest, filter="data")


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
