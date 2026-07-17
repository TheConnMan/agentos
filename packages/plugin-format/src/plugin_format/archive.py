"""Safe archive extraction and bundle-root location -- the single home.

Every lane that unpacks a plugin bundle (the API upload/git-flow path, the
worker's Docker bundle-fetch, the eval-stream suite loader) routes through
``safe_extract`` here so the traversal/symlink/special-file guards live in ONE
audited place. The format is self-sniffed from the bytes (zip, else tar(.gz)),
matching what the callers used to do inline; the upload's stored-key/content-type
concern (``detect_format``) stays with the API where it belongs.

Security model -- an entry is rejected (never extracted) when it:
  * is absolute or contains a ``..`` traversal component, in zip OR tar; or
  * is a link (symlink/hardlink) or a special file (FIFO/device) -- no links at
    all, uniform across zip and tar. Python's ``zipfile`` would materialize a
    symlink-flagged entry as a plain file, so the zip path checks the unix mode
    in the high 16 external-attr bits explicitly (the gap #73 closes).
"""

import io
import stat
import tarfile
import zipfile
from pathlib import Path

from .manifest import resolve_manifest


class UnsupportedArchive(Exception):
    """The bytes are not a recognized zip/tar(.gz), or carry an unsafe entry."""


def _reject_unsafe_path(name: str) -> None:
    """Reject an absolute or traversing member name (shared by zip and tar)."""
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise UnsupportedArchive(f"unsafe path in archive: {name}")


def _extract_zip(data: bytes, dest: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            _reject_unsafe_path(info.filename)
            # A unix symlink is encoded in the high 16 bits of external_attr;
            # non-unix zips leave those bits 0, so this is a no-op for them.
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UnsupportedArchive(f"link entry not allowed in archive: {info.filename}")
        zf.extractall(dest)


def _extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    for m in tf.getmembers():
        _reject_unsafe_path(m.name)
        if m.issym() or m.islnk():
            raise UnsupportedArchive(f"link entry not allowed in archive: {m.name}")
        if not (m.isreg() or m.isdir()):
            raise UnsupportedArchive(f"special entry not allowed in archive: {m.name}")
    # filter="data" (py3.12+) is the traversal/special-file backstop; the
    # pre-scan above is the primary gate and makes "no links at all" explicit.
    tf.extractall(dest, filter="data")


def safe_extract(data: bytes, dest: Path) -> None:
    """Extract ``data`` (zip or tar(.gz)) into ``dest``, refusing unsafe entries.

    Raises ``UnsupportedArchive`` on an unrecognized archive or any absolute,
    traversing, link, or special-file entry -- nothing is written in that case
    beyond what a rejected member's predecessors may have already unpacked.
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        _extract_zip(data, dest)
        return
    for mode in ("r:gz", "r:"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                _extract_tar(tf, dest)
            return
        except tarfile.TarError:
            continue
    raise UnsupportedArchive("data is not a recognized zip or tar(.gz) archive")


def _has_manifest(directory: Path) -> bool:
    return resolve_manifest(directory) is not None


def bundle_root(extracted: Path) -> Path:
    """The directory to validate/mount: unwrap a single top-level folder if present.

    A manifest at the extraction root means the archive was made flat; otherwise
    a common ``tar czf bundle.tgz myplugin/`` wraps everything in one directory,
    so descend into it when that single subdir carries the manifest.
    """
    if _has_manifest(extracted):
        return extracted
    subdirs = [p for p in extracted.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and _has_manifest(subdirs[0]):
        return subdirs[0]
    return extracted
