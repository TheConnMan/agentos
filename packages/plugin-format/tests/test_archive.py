"""Security tests for the single safe archive-extraction home.

These exercise ``plugin_format.safe_extract`` / ``bundle_root`` directly: the
traversal and absolute-path guards, the "no links or special files at all"
rule in BOTH the zip and tar paths (the zip-symlink gap #73 closes), and the
flat/wrapped bundle-root unwrap. Mirrors ``test_validate.py`` style.
"""

import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from plugin_format import (
    DEFAULT_MAX_MEMBERS,
    UnsupportedArchive,
    bundle_root,
    check_archive_bounds,
    safe_extract,
)

MANIFEST = '{"name": "demo-plugin", "version": "0.1.0"}'


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _zip_symlink(name: str, target: str) -> bytes:
    """A zip carrying one entry marked as a unix symlink (high 16 external
    attr bits = S_IFLNK), the shape a malicious archive would use."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo(name)
        zi.external_attr = 0o120777 << 16  # S_IFLNK | 0o777
        zf.writestr(zi, target)
    return buf.getvalue()


def _tar(members: list[tarfile.TarInfo], contents: dict[str, bytes] | None = None) -> bytes:
    contents = contents or {}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for m in members:
            payload = contents.get(m.name)
            tf.addfile(m, io.BytesIO(payload) if payload is not None else None)
    return buf.getvalue()


def _reg(name: str, content: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


def _tar_files(files: dict[str, str], top: str | None = None) -> bytes:
    members: list[tarfile.TarInfo] = []
    contents: dict[str, bytes] = {}
    for rel, text in files.items():
        name = f"{top}/{rel}" if top else rel
        info, payload = _reg(name, text.encode("utf-8"))
        members.append(info)
        contents[name] = payload
    return _tar(members, contents)


# --- traversal + absolute path: zip AND tar ------------------------------


def test_zip_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedArchive):
        safe_extract(_zip({"../escape": "pwn"}), tmp_path)


def test_tar_traversal_rejected(tmp_path: Path) -> None:
    info, payload = _reg("../escape", b"pwn")
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([info], {"../escape": payload}), tmp_path)


def test_zip_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedArchive):
        safe_extract(_zip({"/etc/passwd": "pwn"}), tmp_path)


def test_tar_absolute_path_rejected(tmp_path: Path) -> None:
    info, payload = _reg("/etc/passwd", b"pwn")
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([info], {"/etc/passwd": payload}), tmp_path)


# --- the new coverage: zip symlink entry rejected ------------------------


def test_zip_symlink_entry_rejected(tmp_path: Path) -> None:
    # The gap #73 closes: a zip entry flagged S_IFLNK in its external attrs is
    # a symlink even though Python's zipfile would materialize it as a plain
    # file. Reject it outright so zip matches tar's "no links at all" rule.
    data = _zip_symlink("link", "/etc/passwd")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert stat.S_ISLNK(zf.infolist()[0].external_attr >> 16)  # fixture is a symlink
    with pytest.raises(UnsupportedArchive):
        safe_extract(data, tmp_path)


# --- tar links + special files rejected ----------------------------------


def test_tar_symlink_rejected(tmp_path: Path) -> None:
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([link]), tmp_path)


def test_tar_hardlink_rejected(tmp_path: Path) -> None:
    target, payload = _reg("real", b"data")
    hard = tarfile.TarInfo("link")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "real"
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([target, hard], {"real": payload}), tmp_path)


def test_tar_fifo_special_file_rejected(tmp_path: Path) -> None:
    fifo = tarfile.TarInfo("pipe")
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([fifo]), tmp_path)


def test_tar_device_special_file_rejected(tmp_path: Path) -> None:
    dev = tarfile.TarInfo("dev")
    dev.type = tarfile.CHRTYPE
    dev.devmajor = 1
    dev.devminor = 3
    with pytest.raises(UnsupportedArchive):
        safe_extract(_tar([dev]), tmp_path)


# --- valid bundles extract; bundle_root unwraps --------------------------


def _valid_files() -> dict[str, str]:
    return {
        ".claude-plugin/plugin.json": MANIFEST,
        "skills/greeter/SKILL.md": "---\nname: greeter\ndescription: greets\n---\n",
    }


def test_flat_bundle_extracts_and_root_is_dest(tmp_path: Path) -> None:
    safe_extract(_tar_files(_valid_files()), tmp_path)
    assert (tmp_path / ".claude-plugin" / "plugin.json").is_file()
    assert bundle_root(tmp_path) == tmp_path


def test_wrapped_bundle_root_unwraps_single_dir(tmp_path: Path) -> None:
    safe_extract(_tar_files(_valid_files(), top="demo-plugin"), tmp_path)
    root = bundle_root(tmp_path)
    assert root == tmp_path / "demo-plugin"
    assert (root / ".claude-plugin" / "plugin.json").is_file()


def test_flat_zip_bundle_extracts(tmp_path: Path) -> None:
    safe_extract(_zip(_valid_files()), tmp_path)
    assert bundle_root(tmp_path) == tmp_path
    assert (tmp_path / ".claude-plugin" / "plugin.json").is_file()


# --- non-archive bytes ---------------------------------------------------


def test_non_archive_bytes_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedArchive):
        safe_extract(b"not an archive", tmp_path)


# --- ADR-0059 decision 3: size + compression-ratio bounds ----------------


def _zip_bomb_shaped(name: str = "zeros.bin", size: int = 200_000) -> bytes:
    """A single highly-compressible entry: small on disk, huge once expanded.

    ``size`` zero bytes deflate down to a few hundred bytes, so this trips the
    default 100x compression-ratio cap while staying nowhere near the default
    1 GiB uncompressed-size cap -- isolating the ratio guard from the size
    guard, at a size cheap enough to run in CI (no real multi-GB bomb needed).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, b"\x00" * size)
    return buf.getvalue()


def test_zip_bomb_shaped_archive_is_refused_with_nothing_written(
    tmp_path: Path,
) -> None:
    data = _zip_bomb_shaped()
    # Sanity check the fixture is actually bomb-shaped before asserting the guard.
    assert len(data) < 2_000
    with pytest.raises(UnsupportedArchive, match="compression ratio"):
        safe_extract(data, tmp_path)
    # Nothing was written: the bound check runs before extractall.
    assert list(tmp_path.iterdir()) == []


def test_tar_gz_bomb_shaped_archive_is_refused_with_nothing_written(
    tmp_path: Path,
) -> None:
    info, payload = _reg("zeros.bin", b"\x00" * 200_000)
    data = _tar([info], {"zeros.bin": payload})
    with pytest.raises(UnsupportedArchive, match="compression ratio"):
        safe_extract(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_oversized_uncompressed_total_is_refused(tmp_path: Path) -> None:
    # Random bytes barely compress, so the ratio stays near 1x and only the
    # uncompressed-size cap (set well below the payload here) is exercised.
    import os

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("random.bin", os.urandom(2_000))
    data = buf.getvalue()

    with pytest.raises(UnsupportedArchive, match="1000 byte limit"):
        safe_extract(data, tmp_path, max_uncompressed_bytes=1_000)
    assert list(tmp_path.iterdir()) == []


def test_archive_within_bounds_is_accepted(tmp_path: Path) -> None:
    # A normal small bundle stays well under both defaults.
    safe_extract(_tar_files(_valid_files()), tmp_path)
    assert (tmp_path / ".claude-plugin" / "plugin.json").is_file()


def test_check_archive_bounds_rejects_bomb_without_extracting(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnsupportedArchive, match="compression ratio"):
        check_archive_bounds(_zip_bomb_shaped())
    # check_archive_bounds never takes a dest at all -- nothing to write.


def test_check_archive_bounds_accepts_a_valid_bundle() -> None:
    check_archive_bounds(_tar_files(_valid_files()))  # does not raise


def test_check_archive_bounds_still_rejects_unsafe_entries() -> None:
    # The size/ratio check does not bypass the existing traversal guard.
    with pytest.raises(UnsupportedArchive):
        check_archive_bounds(_zip({"../escape": "pwn"}))


# --- #815: member-count cap, enforced incrementally during the pre-scan ------


def _many_member_zip(count: int) -> bytes:
    """A zip of ``count`` zero-byte members -- the many-empty-member shape."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(count):
            zf.writestr(f"f{i}", b"")
    return buf.getvalue()


def _many_member_tar_gz(count: int) -> bytes:
    """A tar.gz of ``count`` zero-byte members. gzip compresses the repetitive
    headers heavily, so this stays tiny on disk while declaring many members --
    the member-count DoS shape (#815)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(count):
            info = tarfile.TarInfo(f"f{i}")
            info.size = 0
            tf.addfile(info)
    return buf.getvalue()


def test_zip_member_count_over_cap_rejected(tmp_path: Path) -> None:
    data = _many_member_zip(50)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


def test_tar_gz_member_count_over_cap_rejected(tmp_path: Path) -> None:
    data = _many_member_tar_gz(50)
    # Sanity: the archive is genuinely small on disk despite its member count,
    # so only the member-count cap (not the size or ratio cap) can catch it.
    assert len(data) < 4_000
    with pytest.raises(UnsupportedArchive, match="member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


def test_tar_prescan_streams_and_never_calls_getmembers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tar pre-scan must walk the stream one header at a time, never
    ``getmembers()`` (which materializes every member -- and, for a .tar.gz,
    decompresses the whole stream -- up front). Poisoning ``getmembers`` proves
    the incremental walk does not route through it."""

    def _boom(self: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("getmembers() materializes the full member list")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)
    # A normal small bundle still extracts (streaming iteration, no getmembers).
    safe_extract(_tar_files(_valid_files()), tmp_path)
    assert (tmp_path / ".claude-plugin" / "plugin.json").is_file()


def test_tar_gz_member_cap_fires_before_materializing_all_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A many-member tar.gz is refused at the cap without ever building the
    full member list: getmembers() is poisoned, so reaching the cap through the
    streaming walk is the only way this can raise the member-count error."""

    def _boom(self: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("getmembers() materializes the full member list")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        safe_extract(_many_member_tar_gz(50), tmp_path, max_members=10)


def test_tar_gz_declared_huge_member_trips_size_cap_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uncompressed-size cap is enforced incrementally too: a member
    declaring a huge size trips it during the streaming walk (before the walk
    advances past the member and decompresses its body), not after a full
    getmembers() decompress-and-materialize pass."""

    def _boom(self: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("getmembers() decompresses the whole stream up front")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)
    info, payload = _reg("big.bin", b"\x00" * 5_000)
    data = _tar([info], {"big.bin": payload})
    with pytest.raises(UnsupportedArchive, match="byte limit"):
        safe_extract(data, tmp_path, max_uncompressed_bytes=1_000)
    assert list(tmp_path.iterdir()) == []


def test_member_count_within_cap_is_accepted(tmp_path: Path) -> None:
    # A few hundred legitimate members stay well under the default cap.
    safe_extract(_many_member_tar_gz(300), tmp_path, max_members=DEFAULT_MAX_MEMBERS)
    assert len(list(tmp_path.iterdir())) == 300


def test_default_member_cap_is_generous_but_bounded() -> None:
    # Documents the default: hundreds of files pass, hundreds of thousands do not.
    assert DEFAULT_MAX_MEMBERS == 10_000


def test_check_archive_bounds_enforces_member_cap() -> None:
    with pytest.raises(UnsupportedArchive, match="member-count"):
        check_archive_bounds(_many_member_tar_gz(50), max_members=10)
