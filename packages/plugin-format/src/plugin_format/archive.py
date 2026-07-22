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

#815 adds a third bound -- a cap on the **number of members** -- and, crucially,
enforces both it and the uncompressed-size cap INCREMENTALLY during the pre-scan
rather than after the full member list is materialized. The tar pre-scan walks
the stream one header at a time (``for m in tf``) instead of ``getmembers()``,
which for a ``.tar.gz`` would decompress the entire stream up front; so a small
archive with a huge member count, or one declaring a single huge member (a
decompression bomb), is refused mid-walk before hundreds of MiB of TarInfo
objects are built or the bomb's body is decompressed.
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
# A cap on the NUMBER of archive members, enforced INCREMENTALLY during the
# pre-scan so a many-member archive (e.g. a ~5 MiB tar.gz of hundreds of
# thousands of zero-byte members: total uncompressed 0, so it clears both the
# size and ratio caps, yet materializing one TarInfo per member costs hundreds
# of MiB of RSS) is refused mid-walk rather than after the whole member list is
# built (#815). A real plugin bundle (source, skill docs, small fixtures) has
# hundreds to low thousands of files; 10_000 sits well above that and far below
# the hundreds-of-thousands a member-count DoS needs.
DEFAULT_MAX_MEMBERS = 10_000


class UnsupportedArchive(Exception):
    """The bytes are not a recognized zip/tar(.gz), or carry an unsafe entry."""


def _reject_unsafe_path(name: str) -> None:
    """Reject an absolute or traversing member name (shared by zip and tar)."""
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise UnsupportedArchive(f"unsafe path in archive: {name}")


def _reject_over_size(total_uncompressed: int, max_uncompressed_bytes: int) -> None:
    """Reject once the running (or final) uncompressed total crosses the cap.

    Shared by the incremental pre-scan check and ``_check_bounds`` so both
    raise with the identical message: ``total_uncompressed`` is the sum seen so
    far, which is a lower bound on the full extracted footprint, so tripping it
    mid-walk is sound (the real total can only be larger)."""
    if total_uncompressed > max_uncompressed_bytes:
        raise UnsupportedArchive(
            f"archive would extract to {total_uncompressed} bytes, over the "
            f"{max_uncompressed_bytes} byte limit"
        )


def _reject_over_member_count(count: int, max_members: int) -> None:
    """Reject once the running member count crosses the cap. Called per member
    during the pre-scan, before the next member is materialized, so a
    many-member archive is refused mid-walk (#815)."""
    if count > max_members:
        raise UnsupportedArchive(
            f"archive has more than {max_members} members, over the member-count "
            f"limit"
        )


def _check_bounds(
    total_uncompressed: int,
    archive_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> None:
    """Reject an archive whose declared extracted footprint or compression
    ratio exceeds the caps. Called from the pre-scan, before any entry is
    written, so a violation writes nothing to ``dest``. The size cap is also
    enforced incrementally during the pre-scan (see ``_prescan_*``); this
    re-checks it and adds the ratio check, which needs the final total."""
    _reject_over_size(total_uncompressed, max_uncompressed_bytes)
    ratio = total_uncompressed / max(archive_bytes, 1)
    if ratio > max_compression_ratio:
        raise UnsupportedArchive(
            f"archive compression ratio {ratio:.1f}x (uncompressed "
            f"{total_uncompressed} bytes from {archive_bytes} bytes on disk) "
            f"exceeds the {max_compression_ratio}x limit"
        )


def _prescan_zip(
    zf: zipfile.ZipFile,
    *,
    max_uncompressed_bytes: int,
    max_members: int,
) -> int:
    """Reject unsafe entries; return the total declared uncompressed size.

    The member-count and running-uncompressed-size caps are enforced INSIDE the
    loop (before the next entry is touched), so a many-member or declared-huge
    archive is refused mid-walk rather than after the whole list is walked."""
    total = 0
    count = 0
    for info in zf.infolist():
        count += 1
        _reject_over_member_count(count, max_members)
        _reject_unsafe_path(info.filename)
        # A unix symlink is encoded in the high 16 bits of external_attr;
        # non-unix zips leave those bits 0, so this is a no-op for them.
        if stat.S_ISLNK(info.external_attr >> 16):
            raise UnsupportedArchive(f"link entry not allowed in archive: {info.filename}")
        total += info.file_size
        _reject_over_size(total, max_uncompressed_bytes)
    return total


def _prescan_tar(
    tf: tarfile.TarFile,
    *,
    max_uncompressed_bytes: int,
    max_members: int,
) -> int:
    """Reject unsafe entries; return the total declared uncompressed size.

    Iterates the tar as a STREAM (``for m in tf``, one member header at a time)
    rather than ``getmembers()``, which for a ``.tar.gz`` would decompress the
    whole stream to walk every header up front. Combined with the incremental
    member-count and size caps, a many-member archive stops at the cap and a
    declared-huge member (a gzip decompression bomb) trips the size cap BEFORE
    the pre-scan advances past its header and decompresses its body (#815)."""
    total = 0
    count = 0
    for m in tf:
        count += 1
        _reject_over_member_count(count, max_members)
        _reject_unsafe_path(m.name)
        if m.issym() or m.islnk():
            raise UnsupportedArchive(f"link entry not allowed in archive: {m.name}")
        if not (m.isreg() or m.isdir()):
            raise UnsupportedArchive(f"special entry not allowed in archive: {m.name}")
        total += m.size
        _reject_over_size(total, max_uncompressed_bytes)
    return total


def _extract_zip(
    data: bytes,
    dest: Path,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
    max_members: int,
) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = _prescan_zip(
            zf, max_uncompressed_bytes=max_uncompressed_bytes, max_members=max_members
        )
        _check_bounds(total, len(data), max_uncompressed_bytes, max_compression_ratio)
        zf.extractall(dest)


def _extract_tar(
    tf: tarfile.TarFile,
    dest: Path,
    archive_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
    max_members: int,
) -> None:
    total = _prescan_tar(
        tf, max_uncompressed_bytes=max_uncompressed_bytes, max_members=max_members
    )
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
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> None:
    """Extract ``data`` (zip or tar(.gz)) into ``dest``, refusing unsafe entries.

    Raises ``UnsupportedArchive`` on an unrecognized archive, any absolute,
    traversing, link, or special-file entry, or an archive whose member count,
    total uncompressed size, or compression ratio exceeds the given caps --
    nothing is written in that case beyond what a rejected member's predecessors
    may have already unpacked (in practice nothing, since the whole pre-scan,
    including the bound checks, runs before extraction starts). The member-count
    and uncompressed-size caps are enforced incrementally during the pre-scan,
    so a many-member or decompression-bomb archive is refused mid-walk rather
    than after the full member list is materialized.
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        _extract_zip(
            data, dest, max_uncompressed_bytes, max_compression_ratio, max_members
        )
        return
    for mode in ("r:gz", "r:"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                _extract_tar(
                    tf,
                    dest,
                    len(data),
                    max_uncompressed_bytes,
                    max_compression_ratio,
                    max_members,
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
    max_members: int = DEFAULT_MAX_MEMBERS,
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
            total = _prescan_zip(
                zf,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_members=max_members,
            )
        _check_bounds(total, len(data), max_uncompressed_bytes, max_compression_ratio)
        return
    for mode in ("r:gz", "r:"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
                total = _prescan_tar(
                    tf,
                    max_uncompressed_bytes=max_uncompressed_bytes,
                    max_members=max_members,
                )
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
