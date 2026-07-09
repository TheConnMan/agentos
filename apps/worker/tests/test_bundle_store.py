"""safe_extract: the single worker archive extractor rejects unsafe bundles.

Pure unit tests (no MinIO / stack), but built from the *real* situation rather
than hand-crafted attribute bits: a plugin author zips or tars a working tree
that happens to contain a symlink pointing outside the bundle (a careless
``ln -s``, a checked-in link, or a deliberate escape). The link's name is
innocent (no ``..``), so a name-only guard would pass it, yet extracting it would
place a link that reads a file outside the bundle root. We build the archives the
way a real symlink-aware archiver does -- from actual on-disk symlinks -- and
assert extraction refuses them while a normal bundle of the same shape extracts.
``eval.stream`` imports this same ``safe_extract`` (no second copy).
"""

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from agentos_worker.bundle_store import extract_bundle, safe_extract


def _zip_from_dir(root: Path) -> bytes:
    """Zip a tree the way ``zip --symlinks`` does: a real symlink is stored as a
    symlink entry (its unix mode in ``external_attr``), not its target's bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(rel)
                info.external_attr = os.lstat(path).st_mode << 16
                zf.writestr(info, os.readlink(path))
            elif path.is_file():
                zf.write(path, rel)
    return buf.getvalue()


def _tar_gz_from_dir(root: Path) -> bytes:
    """tar a tree; ``tarfile`` stores a real on-disk symlink as a symlink member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(root, arcname=".", recursive=True)
    return buf.getvalue()


def _bundle_tree_with_symlink(tmp_path: Path) -> Path:
    """A working tree a plugin author might archive: a real manifest plus a
    symlink whose name is innocent but which points outside the bundle."""
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("credentials that must not leak")
    work = tmp_path / "work"
    (work / ".claude-plugin").mkdir(parents=True)
    (work / ".claude-plugin" / "plugin.json").write_text('{"name": "x", "version": "0.1.0"}')
    os.symlink(outside, work / "config.json")  # innocent name, escapes the tree
    return work


def test_zip_bundle_carrying_a_symlink_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="symlink"):
        safe_extract(_zip_from_dir(_bundle_tree_with_symlink(tmp_path)), out)
    # Nothing was materialized at the innocent link name, so the outside file is
    # not reachable through the extracted bundle.
    assert not (out / "config.json").exists()


def test_tar_bundle_carrying_a_symlink_is_rejected(tmp_path: Path) -> None:
    # The tar path relies on tarfile filter="data"; the extractor surfaces its
    # rejection as an unsafe-entry error rather than "unrecognized archive".
    with pytest.raises(ValueError, match="unsafe entry"):
        safe_extract(_tar_gz_from_dir(_bundle_tree_with_symlink(tmp_path)), tmp_path / "out")


def test_normal_bundle_extracts(tmp_path: Path) -> None:
    # Same shape without the escape: a real config file (not a symlink) extracts.
    work = tmp_path / "work"
    (work / ".claude-plugin").mkdir(parents=True)
    (work / ".claude-plugin" / "plugin.json").write_text('{"name": "x", "version": "0.1.0"}')
    (work / "config.json").write_text("{}")
    out = tmp_path / "out"
    safe_extract(_zip_from_dir(work), out)
    assert (out / "config.json").read_text() == "{}"
    assert (out / ".claude-plugin" / "plugin.json").is_file()


def test_extract_bundle_rejects_a_symlink_bundle(tmp_path: Path) -> None:
    # extract_bundle (the Docker-substrate entrypoint) delegates to the one
    # safe_extract, so the guard applies to the runtime path too.
    with pytest.raises(ValueError):
        extract_bundle(_zip_from_dir(_bundle_tree_with_symlink(tmp_path)), tmp_path / "out")


def test_zip_traversal_is_rejected(tmp_path: Path) -> None:
    # A `..` entry is the other escape shape; keep the name-guard covered too.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe path"):
        safe_extract(buf.getvalue(), tmp_path / "out")
