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

ADR-0059 decision 3 adds two more bounds, checked in the same pre-scan pass
before anything is written: the archive's **total uncompressed size** and its
**compression ratio** (total uncompressed bytes / bytes of the archive itself).
Both are a full-archive refusal, not a partial unpack -- an archive that would
exceed either is rejected before ``extractall``/``tf.extractall`` ever runs, so
a zip-bomb-shaped archive writes nothing to disk. The ratio is computed against
the whole archive's byte length rather than a per-entry compressed size because
tar(.gz) compresses the whole stream, not per-member, so per-member compressed
sizes do not exist there; using the same denominator for zip keeps one uniform
rule across both formats.
"""

import io
import stat
import tarfile
import zipfile
from pathlib import Path

from .manifest import resolve_manifest

# Generous defaults: they bound a runaway archive, not ordinary bundle content
# (source, skill docs, small fixtures). Both are operator-overridable by every
# caller (the API's Settings, the worker's WorkerConfig) rather than fixed here;
# these are only the fallback for a caller that does not pass its own.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024  # 1 GiB
# A legitimate source/text bundle rarely compresses past ~10x; a classic zip
# bomb (e.g. a large run of a single repeated byte) routinely clears 1000x.
# 100x sits well above real bundles and well below a bomb's typical ratio.
DEFAULT_MAX_COMPRESSION_RATIO = 100.0


class UnsupportedArchive(Exception):
    """The bytes are not a recognized zip/tar(.gz), or carry an unsafe entry."""


def _reject_unsafe_path(name: str) -> None:
    """Reject an absolute or traversing member name (shared by zip and tar)."""
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise UnsupportedArchive(f"unsafe path in archive: {name}")


def _check_bounds(
    total_uncompressed: int,
    archive_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> None:
    """Reject an archive whose declared extracted footprint or compression
    ratio exceeds the caps. Called from the pre-scan, before any entry is
    written, so a violation writes nothing to ``dest``."""
    if total_uncompressed > max_uncompressed_bytes:
        raise UnsupportedArchive(
            f"archive would extract to {total_uncompressed} bytes, over the "
            f"{max_uncompressed_bytes} byte limit"
        )
    ratio = total_uncompressed / max(archive_bytes, 1)
    if ratio > max_compression_ratio:
        raise UnsupportedArchive(
            f"archive compression ratio {ratio:.1f}x (uncompressed "
            f"{total_uncompressed} bytes from {archive_bytes} bytes on disk) "
            f"exceeds the {max_compression_ratio}x limit"
        )


def _prescan_zip(zf: zipfile.ZipFile) -> int:
    """Reject unsafe entries; return the total declared uncompressed size."""
    total = 0
    for info in zf.infolist():
        _reject_unsafe_path(info.filename)
        # A unix symlink is encoded in the high 16 bits of external_attr;
        # non-unix zips leave those bits 0, so this is a no-op for them.
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UnsupportedArchive(f"link entry not allowed in archive: {info.filename}")
        total += info.file_size
    return total


def _prescan_tar(tf: tarfile.TarFile) -> int:
    """Reject unsafe entries; return the total declared uncompressed size."""
    total = 0
    for m in tf.getmembers():
        _reject_unsafe_path(m.name)
        if m.issym() or m.islnk():
            raise UnsupportedArchive(f"link entry not allowed in archive: {m.name}")
        if not (m.isreg() or m.isdir()):
            raise UnsupportedArchive(f"special entry not allowed in archive: {m.name}")
        total += m.size
    return total


def _extract_zip(
    data: bytes, dest: Path, max_uncompressed_bytes: int, max_compression_ratio: float
) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = _prescan_zip(zf)
        _check_bounds(total, len(data), max_uncompressed_bytes, max_compression_ratio)
        zf.extractall(dest)


def _extract_tar(
    tf: tarfile.TarFile,
    dest: Path,
    archive_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> None:
    total = _prescan_tar(tf)
    _check_bounds(total, archive_bytes, max_uncompressed_bytes, max_compression_ratio)
    # filter="data" (py3.12+) is the traversal/special-file backstop; the
    # pre-scan above is the primary gate and makes "no links at all" explicit.
    tf.extractall(dest, filter="data")


def safe_extract(
    data: bytes,
    dest: Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    """Extract ``data`` (zip or tar(.gz)) into ``dest``, refusing unsafe entries.

    Raises ``UnsupportedArchive`` on an unrecognized archive, any absolute,
    traversing, link, or special-file entry, or an archive whose total
    uncompressed size or compression ratio exceeds the given caps -- nothing is
    written in that case beyond what a rejected member's predecessors may have
    already unpacked (in practice nothing, since the whole pre-scan, including
    the bound check, runs before extraction starts).
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        _extract_zip(data, dest, max_uncompressed_bytes, max_compression_ratio)
        return
    for mode in ("r:gz", "r:"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                _extract_tar(
                    tf, dest, len(data), max_uncompressed_bytes, max_compression_ratio
                )
            return
        except tarfile.TarError:
            continue
    raise UnsupportedArchive("data is not a recognized zip or tar(.gz) archive")


def check_archive_bounds(
    data: bytes,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> None:
    """Validate an archive's size/ratio bounds without extracting anything.

    Runs the identical safety pre-scan ``safe_extract`` runs before unpacking
    (unsafe paths, links, special files, uncompressed-size and
    compression-ratio caps) but stops there -- nothing is ever written to disk,
    so this is cheap to run against an already-stored bundle's bytes.

    This is what a deploy-time revalidation of an already-stored bundle calls
    (ADR-0059 decision 3's backward-compatibility commitment): a bundle stored
    under a previous, looser (or absent) cap is checked against the CURRENT
    caps before it is handed to a sandbox substrate to fetch and extract, so an
    oversized legacy bundle fails here with a clear, actionable error instead of
    surfacing as an opaque init-container failure or a mid-extract eviction on
    the node.
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = _prescan_zip(zf)
        _check_bounds(total, len(data), max_uncompressed_bytes, max_compression_ratio)
        return
    for mode in ("r:gz", "r:"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                total = _prescan_tar(tf)
            _check_bounds(total, len(data), max_uncompressed_bytes, max_compression_ratio)
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
