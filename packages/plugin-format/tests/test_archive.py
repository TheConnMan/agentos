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
from collections.abc import Callable
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


# --- #815 (verification follow-up): the zip cap must fire from the EOCD BEFORE
# `zipfile.ZipFile` parses and materializes the whole central directory. The
# in-loop cap in `_prescan_zip` runs only AFTER construction, so a many-member
# zip was fully materialized (hundreds of MiB of RSS) before it could fire. ---


def _boom_zipfile(*_args: object, **_kwargs: object) -> zipfile.ZipFile:
    raise AssertionError("ZipFile() materializes the whole central directory")


def test_zip_member_cap_fires_before_opening_the_zipfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A many-member zip is refused from the EOCD record BEFORE
    ``zipfile.ZipFile`` is constructed. Poisoning ``ZipFile`` proves the reject
    comes from the pre-check, not the post-construction in-loop cap (the
    module's ``is_zipfile`` detection uses ``_EndRecData``, not the class, so it
    is unaffected)."""
    data = _many_member_zip(50)  # build BEFORE poisoning ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


def test_check_archive_bounds_zip_member_cap_fires_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deploy-time revalidation path enforces the same pre-open cap."""
    data = _many_member_zip(50)  # build BEFORE poisoning ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        check_archive_bounds(data, max_members=10)


def test_zip_within_cap_still_opens_and_extracts(tmp_path: Path) -> None:
    """No false reject: a zip whose member count is within the cap, even with
    long paths, opens and extracts normally (the EOCD pre-check is not tripped
    by a legitimate near-cap bundle)."""
    entries = {f"src/deep/nested/component/module_{i}.py": "" for i in range(300)}
    safe_extract(_zip(entries), tmp_path, max_members=DEFAULT_MAX_MEMBERS)
    assert len(list(tmp_path.rglob("*.py"))) == 300


def _synthetic_eocd(total_entries: int, size_cd: int, comment: bytes = b"") -> bytes:
    """A minimal, well-formed end-of-central-directory record (no central
    directory body needed -- the pre-check reads only the EOCD's own fields)."""
    import struct

    return (
        struct.pack(
            "<4sHHHHIIH",
            b"\x50\x4b\x05\x06",  # EOCD signature
            0,  # disk number
            0,  # disk with central directory
            min(total_entries, 0xFFFF),  # entries on this disk
            min(total_entries, 0xFFFF),  # total entries
            min(size_cd, 0xFFFFFFFF),  # central directory size
            0,  # central directory offset
            len(comment),
        )
        + comment
    )


def test_zip_eocd_precheck_rejects_zip64_sentinel() -> None:
    """An entry count at the 16-bit ZIP64 sentinel (0xFFFF) means the true
    count overflows the field -- far past any sane cap -- so it is rejected
    without consulting the (absent) ZIP64 record."""
    from plugin_format.archive import _reject_zip_over_member_count

    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        _reject_zip_over_member_count(_synthetic_eocd(0xFFFF, 100), max_members=10_000)


def test_zip_eocd_precheck_rejects_under_declared_count_via_size() -> None:
    """The size guard defeats an under-declared count: an EOCD claiming only a
    handful of entries but whose central-directory SIZE has room for far more
    is rejected (``zipfile`` parses by size, not by the declared count, so the
    size is the signal that actually bounds its work)."""
    from plugin_format.archive import (
        _ZIP_CENTRAL_DIR_BYTES_PER_MEMBER,
        _reject_zip_over_member_count,
    )

    big_size_cd = 10_000 * _ZIP_CENTRAL_DIR_BYTES_PER_MEMBER + 1
    with pytest.raises(UnsupportedArchive, match="member-count"):
        _reject_zip_over_member_count(
            _synthetic_eocd(total_entries=5, size_cd=big_size_cd), max_members=10_000
        )


def test_zip_eocd_precheck_allows_within_cap() -> None:
    """A well-formed EOCD within both the count and size bounds passes."""
    from plugin_format.archive import _reject_zip_over_member_count

    _reject_zip_over_member_count(_synthetic_eocd(total_entries=42, size_cd=5_000), 10_000)


def test_zip_eocd_precheck_ignores_a_spurious_trailing_signature() -> None:
    """A later byte run that itself starts with the EOCD signature is the record
    ``rfind`` now locates (matching CPython, which rfinds the LAST signature in
    the tail window). Here that embedded run is a full 22-byte pseudo-record
    whose entry-count bytes are 0, so reading it keeps the archive under the cap
    and the guard does NOT raise."""
    from plugin_format.archive import _reject_zip_over_member_count

    # A 22-byte run starting with the signature, appended as the "comment": rfind
    # lands on it and it is unpacked in place as the record (entry count 0).
    fake = b"\x50\x4b\x05\x06" + b"\x00" * 18  # a full 22-byte pseudo-record
    data = _synthetic_eocd(total_entries=42, size_cd=5_000, comment=fake)
    # The later fake (0 entries, within cap) is the record rfind locates, so this
    # does NOT raise.
    _reject_zip_over_member_count(data, max_members=10_000)


# --- #848: the EOCD locator must be as lenient as CPython's `_EndRecData`.
# The guard required the declared comment to land exactly at end-of-tail, a
# rule CPython does not have: `_EndRecData` rfinds the signature, unpacks the
# 22-byte record there, and uses the declared comment size only to SLICE the
# comment (`comment = data[start+sizeEndCentDir:start+sizeEndCentDir+commentSize]`
# in Lib/zipfile/__init__.py), accepting a short slice silently. So one
# appended byte, or an over-declared comment_len, made the guard return without
# raising while `zipfile.ZipFile` still opened the archive and materialized the
# whole central directory -- the #815 DoS, reopened. ---


def _append_junk_byte(data: bytes) -> bytes:
    """One trailing byte after the EOCD: the record no longer ends at EOF."""
    return data + b"\x00"


def _over_declare_comment_len(data: bytes) -> bytes:
    """Claim a 5-byte archive comment that is not there. A zip written without
    a comment ends with the EOCD's own 2-byte comment_len field, so the final
    two bytes are the field to patch."""
    assert data[-2:] == b"\x00\x00"
    return data[:-2] + (5).to_bytes(2, "little")


EOCD_MUTATIONS = [
    pytest.param(_append_junk_byte, id="appended-junk-byte"),
    pytest.param(_over_declare_comment_len, id="over-declared-comment-len"),
]


@pytest.mark.parametrize("mutate", EOCD_MUTATIONS)
def test_eocd_mutated_zip_is_still_opened_by_zipfile(mutate: Callable[[bytes], bytes]) -> None:
    """The premise of the bypass, asserted by execution rather than assumed:
    CPython still opens these archives and still sees every member, so the
    guard must fire for them. If CPython ever tightens, this fails loudly and
    tells the reader the threat model changed."""
    data = mutate(_many_member_zip(50))
    assert zipfile.is_zipfile(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.infolist()) == 50


@pytest.mark.parametrize("mutate", EOCD_MUTATIONS)
def test_safe_extract_rejects_eocd_mutated_over_cap_zip(
    mutate: Callable[[bytes], bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real over-cap zip, mutated after the fact, is still refused from the
    EOCD BEFORE ``zipfile.ZipFile`` is constructed. ``ZipFile`` is poisoned
    because the post-construction in-loop cap raises the identical message, so
    without the poison this would pass on the bypassed path (#815's property is
    pre-open refusal, not merely some error)."""
    data = mutate(_many_member_zip(50))  # build BEFORE poisoning ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutate", EOCD_MUTATIONS)
def test_check_archive_bounds_rejects_eocd_mutated_over_cap_zip(
    mutate: Callable[[bytes], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy-time revalidation entry point reaches the same guard, so the
    mutated archive must be refused there too, also before construction."""
    data = mutate(_many_member_zip(50))  # build BEFORE poisoning ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        check_archive_bounds(data, max_members=10)


def _sentinel_entry_counts(data: bytes) -> bytes:
    """Patch a real comment-less zip's EOCD entry-count fields (bytes 8:10 and
    10:12 of the record) to the 16-bit ZIP64 sentinel."""
    start = len(data) - 22
    assert data[start : start + 4] == b"\x50\x4b\x05\x06"
    return data[: start + 8] + b"\xff\xff\xff\xff" + data[start + 12 :]


def test_safe_extract_rejects_zip64_sentinel_with_appended_junk_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The appended byte bypassed the ZIP64 sentinel branch as well. The
    archive still opens under CPython with every member present, so the
    sentinel refusal must survive the mutation and must still precede
    construction."""
    data = _append_junk_byte(_sentinel_entry_counts(_many_member_zip(50)))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.infolist()) == 50
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


def test_honest_commented_zip_within_cap_still_extracts(tmp_path: Path) -> None:
    """Negative control: a legitimate under-cap zip carrying a real, correctly
    declared archive comment extracts with its contents intact."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.comment = b"built by a real packer"
        zf.writestr("src/app.py", "print('hello')")
        zf.writestr("README.md", "docs")
    safe_extract(buf.getvalue(), tmp_path, max_members=10)
    assert (tmp_path / "src" / "app.py").read_text() == "print('hello')"
    assert (tmp_path / "README.md").read_text() == "docs"


# --- #848 (round 2): two review findings against the plain-rfind locator. ---
#
# Finding 1 (code-reviewer MAJOR): CPython's `_EndRecData` has a FAST PATH
# before its rfind search. It reads the last 22 bytes and, if they start with
# `PK\x05\x06` and end with b"\x00\x00" (comment length zero), unpacks them in
# place and never searches -- even when the signature bytes recur later inside
# the record's own fields (Lib/zipfile/__init__.py; verified on CPython 3.14.3
# via `inspect.getsource(zipfile._EndRecData)`). The guard's plain
# `tail.rfind(signature)` instead diverts to that in-field occurrence, leaves
# <22 bytes after it, and wrongly reports "no readable end-of-central-directory
# record" for an archive CPython opens fine.
#
# Finding 2 (security HIGH): `_EndRecData` then calls `_EndRecData64`
# UNCONDITIONALLY (not gated on the 0xFFFF/0xFFFFFFFF sentinel). `_EndRecData64`
# looks exactly 20 bytes before the located 32-bit EOCD for a ZIP64 EOCD locator
# signature `PK\x06\x07` (`zipfile.stringEndArchive64Locator`,
# `zipfile.sizeEndCentDir64Locator == 20`); if present it reads the 64-bit count
# from the ZIP64 EOCD record and OVERRIDES the 32-bit fields (verified via
# `inspect.getsource(zipfile._EndRecData64)`). So an attacker who patches the
# 32-bit EOCD counts below the sentinel while leaving the ZIP64 locator intact
# bypasses a guard that only inspects the 32-bit sentinel -- CPython still reads
# the real (huge) count and materializes every member.


def _fast_path_eocd_with_signature_in_offset_field(total_entries: int) -> bytes:
    """A comment-less 32-bit EOCD (trailing 22 bytes: signature + fields +
    comment_len 0, so they end b"\x00\x00") whose offset-of-central-directory
    field (record bytes 16:20) itself holds the EOCD signature bytes.

    A plain rfind of the signature lands on that in-field occurrence, not the
    real trailing record. CPython does NOT: its `_EndRecData` fast path reads
    the last 22 bytes directly when they start with `PK\x05\x06` and end
    b"\x00\x00", so the recurring bytes inside the record never divert it.

    A direct byte construction is the correct tool here (not the `zipfile`
    module): the assertion is about the guard's LOCATOR against a hostile-but-
    CPython-valid tail, and CPython's fast-path behavior is cited from its
    source above rather than re-derived from a written archive.
    """
    import struct

    offset_cd = int.from_bytes(b"\x50\x4b\x05\x06", "little")
    record = struct.pack(
        "<4sHHHHIIH",
        b"\x50\x4b\x05\x06",  # EOCD signature
        0,  # disk number
        0,  # disk with central directory
        total_entries,  # entries on this disk
        total_entries,  # total entries
        46,  # central directory size (non-sentinel)
        offset_cd,  # offset field (bytes 16:20) == the signature bytes
        0,  # comment length 0 -> record ends b"\x00\x00" (fast path applies)
    )
    assert record[16:20] == b"\x50\x4b\x05\x06"  # signature recurs in-field
    assert record[-2:] == b"\x00\x00"  # comment-less: CPython fast path applies
    # A little filler before the record so the true record is the trailing 22.
    return b"\x00\x00\x00\x00" + record


def test_eocd_locator_uses_cpython_fast_path_when_signature_recurs_in_fields() -> None:
    """Finding 1: the guard must locate the record the way CPython's fast path
    does. This trailing 22-byte EOCD declares 3 entries (under the cap), but its
    offset field replays the signature, so a plain rfind diverts to the in-field
    hit, leaves <22 bytes, and falsely reports no EOCD. The guard must read the
    true trailing record and NOT raise."""
    from plugin_format.archive import _reject_zip_over_member_count

    data = _fast_path_eocd_with_signature_in_offset_field(total_entries=3)
    # Premise, by execution: CPython locates the real EOCD via its fast path.
    assert zipfile.is_zipfile(io.BytesIO(data))
    _reject_zip_over_member_count(data, max_members=10)  # must not raise


def test_eocd_fast_path_record_over_cap_is_still_rejected() -> None:
    """Companion to the above: the fast-path record's real counts still gate.
    The same trailing-22 EOCD declaring 50 entries against a cap of 10 must be
    rejected for member count -- proof the guard reads the true record's fields,
    not merely that it stopped raising the no-EOCD error."""
    from plugin_format.archive import _reject_zip_over_member_count

    data = _fast_path_eocd_with_signature_in_offset_field(total_entries=50)
    with pytest.raises(UnsupportedArchive, match="member-count"):
        _reject_zip_over_member_count(data, max_members=10)


# 65536 members: one past ZIP_FILECOUNT_LIMIT (0xFFFF), the minimum count that
# makes CPython emit a ZIP64 EOCD record + locator and set the 32-bit EOCD
# entry-count fields to the 0xFFFF sentinel. Kept at the minimum to stay fast
# (builds and opens in well under a second on CPython 3.14.3).
_ZIP64_FORCING_MEMBER_COUNT = 65536


def _zip64_override_patched_under_cap(data: bytes) -> bytes:
    """Patch a real ZIP64 zip's trailing 32-bit EOCD total_entries (record bytes
    10:12) and size_cd (bytes 12:16) down to small NON-sentinel values (1 and
    46), leaving the `PK\x06\x06` ZIP64 EOCD record and the `PK\x06\x07` locator
    intact. A guard that only inspects the 32-bit sentinel now sees a tiny
    archive; CPython reads the real count from the ZIP64 record and still
    materializes every member."""
    start = len(data) - 22  # comment-less: the 32-bit EOCD is the trailing 22
    assert data[start : start + 4] == b"\x50\x4b\x05\x06"
    patched = bytearray(data)
    patched[start + 10 : start + 12] = (1).to_bytes(2, "little")  # total_entries
    patched[start + 12 : start + 16] = (46).to_bytes(4, "little")  # size_cd
    return bytes(patched)


def test_eocd_zip64_locator_override_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: an attacker patches the 32-bit EOCD counts below the sentinel
    while leaving the ZIP64 locator intact. CPython's `_EndRecData64` (called
    unconditionally, 20 bytes before the 32-bit EOCD) reads the real huge count
    from the ZIP64 record and materializes every member, so the guard must
    reject on the ZIP64 structure rather than trust the patched 32-bit fields.

    ``ZipFile`` is poisoned (as in the #815 tests) so the refusal must come from
    the PRE-open EOCD guard: the property is pre-open refusal, not merely some
    error. Without the poison the post-open in-loop cap in ``_prescan_zip`` would
    raise the identical message AFTER CPython materialized all 65536 members --
    exactly the DoS the guard exists to prevent."""
    raw = _many_member_zip(_ZIP64_FORCING_MEMBER_COUNT)
    data = _zip64_override_patched_under_cap(raw)  # build BEFORE poisoning ZipFile
    start = len(data) - 22
    # Premise, by execution: CPython opens it and sees ALL members, and the
    # `PK\x06\x07` locator sits exactly 20 bytes before the 32-bit EOCD (the
    # offset `_EndRecData64` reads, sizeEndCentDir64Locator == 20).
    assert zipfile.is_zipfile(io.BytesIO(data))
    assert data[start - 20 : start - 16] == b"\x50\x4b\x06\x07"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.infolist()) == _ZIP64_FORCING_MEMBER_COUNT
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []


def test_check_archive_bounds_rejects_zip64_locator_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deploy-time revalidation path reaches the same pre-open guard, so the
    patched ZIP64 archive must be refused there too, also before construction."""
    data = _zip64_override_patched_under_cap(
        _many_member_zip(_ZIP64_FORCING_MEMBER_COUNT)
    )  # build BEFORE poisoning ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        check_archive_bounds(data, max_members=10)


# The #848 appended-junk vector applied to the ZIP64 branch. The prior ZIP64
# tests place the `PK\x06\x07` locator 20 bytes before a trailing 32-bit EOCD
# that sits at end-of-file, so it lands well inside the guard's tail window and
# `start >= 20` holds. But the guard slices the locator TAIL-relative
# (`tail[start-20:...]`) and gates the ZIP64 check on `start >= 20`. Appending
# junk AFTER the 32-bit EOCD (the same lever #815/#848 used to slide the record)
# pushes the located EOCD toward the FRONT of the ~65558-byte tail window: with
# ~65520 trailing bytes the EOCD lands at tail offset ~16, so `start < 20`, the
# locator (now at tail offset ~-4, before the slice) is never inspected, and the
# guard trusts the attacker-patched 32-bit fields and stays silent. CPython
# reads the locator via an ABSOLUTE seek 20 bytes before the located EOCD, so it
# still overrides with the real 64-bit count and materializes all 65536 members.
_ZIP64_LOCATOR_BEYOND_WINDOW_JUNK = 65520


def _zip64_override_patched_under_cap_beyond_window(data: bytes) -> bytes:
    """Patch the 32-bit EOCD counts down (as above) then append junk AFTER the
    EOCD so the real record slides into the first ~20 bytes of the guard's tail
    window while CPython's rfind still lands it. The `PK\x06\x07` locator is left
    intact 20 bytes before the (now non-trailing) 32-bit EOCD."""
    return _zip64_override_patched_under_cap(data) + b"\x00" * _ZIP64_LOCATOR_BEYOND_WINDOW_JUNK


def test_eocd_zip64_locator_override_beyond_tail_window_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ZIP64 locator override still bypasses the guard when trailing junk
    pushes the located 32-bit EOCD into the first 20 bytes of the tail window:
    the guard's `start >= 20` gate then skips the locator check entirely, yet
    CPython (absolute seek) reads the 64-bit count and opens every member.

    ``ZipFile`` is poisoned (as in the sibling ZIP64 tests) so the refusal must
    come from the PRE-open EOCD guard, not the post-open in-loop cap that would
    fire only AFTER CPython materialized all 65536 members -- the DoS itself."""
    data = _zip64_override_patched_under_cap_beyond_window(
        _many_member_zip(_ZIP64_FORCING_MEMBER_COUNT)
    )  # build BEFORE poisoning ZipFile
    start = len(data) - 22 - _ZIP64_LOCATOR_BEYOND_WINDOW_JUNK
    # Premise, by execution: CPython opens it and sees ALL members, and the
    # `PK\x06\x07` locator is still present 20 bytes before the real 32-bit EOCD.
    assert zipfile.is_zipfile(io.BytesIO(data))
    assert data[start - 20 : start - 16] == b"\x50\x4b\x06\x07"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.infolist()) == _ZIP64_FORCING_MEMBER_COUNT
    monkeypatch.setattr(zipfile, "ZipFile", _boom_zipfile)
    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        safe_extract(data, tmp_path, max_members=10)
    assert list(tmp_path.iterdir()) == []
    # Same bytes, same pre-open guard: the deploy-time revalidation path must
    # refuse before it constructs the (still poisoned) ZipFile.
    with pytest.raises(UnsupportedArchive, match="ZIP64|member-count"):
        check_archive_bounds(data, max_members=10)
