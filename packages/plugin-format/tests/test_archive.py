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
from plugin_format import UnsupportedArchive, bundle_root, safe_extract

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
