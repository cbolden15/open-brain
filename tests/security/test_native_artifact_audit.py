"""Synthetic executable containers; bundled code is data and must never run."""

from __future__ import annotations

import importlib.util
import io
import marshal
import struct
import subprocess
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

import pytest

from tools.open_brain_dev import artifact_audit as native
from tools.open_brain_dev.release_audit import content_rule_ids, main

TERM = "synthetic_owner_marker"


def module(text: str = "pass", filename: str = "fixture.py") -> bytes:
    return marshal.dumps(compile(text, filename, "exec"))


def pyz(code: bytes, name: str = "fixture") -> bytes:
    packed = zlib.compress(code)
    return (
        b"PYZ\0"
        + importlib.util.MAGIC_NUMBER
        + struct.pack("!I", 17 + len(packed))
        + bytes(5)
        + packed
        + marshal.dumps([(name, (0, 17, len(packed)))])
    )


def package(entries: list[tuple[str, bytes, bytes]], *, option: bool = True) -> bytes:
    payload = bytearray()
    toc = bytearray()
    for name, kind, value in entries:
        packed = zlib.compress(value)
        label = name.encode() + b"\0"
        toc += (
            native.ENTRY.pack(
                native.ENTRY.size + len(label), len(payload), len(packed), len(value), 1, kind
            )
            + label
        )
        payload += packed
    if option:
        label = b"pyi-contents-directory _internal\0"
        toc += (
            native.ENTRY.pack(native.ENTRY.size + len(label), len(payload), 0, 0, 0, b"o") + label
        )
    return bytes(payload + toc) + native.COOKIE.pack(
        native.COOKIE_MAGIC,
        len(payload) + len(toc) + native.COOKIE.size,
        len(payload),
        len(toc),
        314,
        b"libpython3.14.so",
    )


def executable(pkg: bytes, platform: str, prefix: bytes = b"") -> bytes:
    if platform == "linux-x86_64":
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, 62)
        names = b"\0pydata\0.shstrtab\0"
        start = 64 + len(prefix)
        table = start + len(pkg) + len(names)
        struct.pack_into("<Q", header, 40, table)
        struct.pack_into("<HHH", header, 58, 64, 3, 2)
        rows = (
            bytes(64)
            + struct.pack("<IIQQQQIIQQ", 1, 1, 0, 0, start, len(pkg), 0, 0, 1, 0)
            + struct.pack("<IIQQQQIIQQ", 8, 3, 0, 0, start + len(pkg), len(names), 0, 0, 1, 0)
        )
        return bytes(header) + prefix + pkg + names + rows
    header = bytearray(32)
    header[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<I", header, 4, 0x100000C)
    struct.pack_into("<II", header, 16, 1, 16)
    signature = b"synthetic-signature"
    return (
        bytes(header)
        + struct.pack("<IIII", 0x1D, 16, 48 + len(prefix) + len(pkg), len(signature))
        + prefix
        + pkg
        + signature
    )


def archive(binary: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
        entry = tarfile.TarInfo("open-brain")
        entry.size = len(binary)
        entry.mode = 0o755
        tar.addfile(entry, io.BytesIO(binary))
    return buffer.getvalue()


def scan(
    entries: list[tuple[str, bytes, bytes]], platform: str = "linux-x86_64", prefix: bytes = b""
) -> native.Scanner:
    scanner = native.Scanner([TERM])
    scanner.archive(
        archive(executable(package(entries), platform, prefix)),
        f"open-brain-0.1.0-{platform}.tar.gz",
    )
    return scanner


@pytest.mark.parametrize("platform", ["linux-x86_64", "macos-arm64"])
def test_large_native_is_inspected_without_relaxing_source_limit(platform: str) -> None:
    padding = b"X" * (3 * 1024 * 1024)
    assert not scan([("PYZ.pyz", b"z", pyz(module()))], platform, padding).findings
    assert "content-scan-limit-exceeded" in content_rule_ids(padding, [TERM])


@pytest.mark.parametrize(
    "surface", ["loader", "constant", "filename", "module-name", "member", "link", "library"]
)
def test_denylist_reaches_every_native_layer(surface: str) -> None:
    code = module(
        f"value = {TERM!r}" if surface == "constant" else "pass",
        TERM if surface == "filename" else "fixture.py",
    )
    entries = [("PYZ.pyz", b"z", pyz(code, TERM if surface == "module-name" else "fixture"))]
    if surface == "member":
        entries.append(("data.txt", b"x", TERM.encode()))
    if surface == "link":
        entries.extend([(TERM, b"x", b"ok"), ("alias", b"n", TERM.encode() + b"\0")])
    if surface == "library":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "fixture.pyc", importlib.util.MAGIC_NUMBER + bytes(12) + module(f"value = {TERM!r}")
            )
        entries.append(("base_library.zip", b"x", buffer.getvalue()))
    scanner = scan(entries, prefix=TERM.encode() if surface == "loader" else b"")
    assert "private-denylist-term" in {rule for _, rule in scanner.findings}


def test_unicode_constants_and_generic_rules_are_scanned() -> None:
    marker = "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in TERM)
    address = ".".join(["192", "168", "5", "8"])
    scanner = scan([("PYZ.pyz", b"z", pyz(module(f"a = {marker!r}; b = {address!r}")))])
    assert {"private-denylist-term", "private-ip-address"} <= {r for _, r in scanner.findings}


def test_bundled_code_is_never_executed(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist"
    code = module(f"from pathlib import Path; Path({str(output)!r}).touch()")
    scan([("PYZ.pyz", b"z", pyz(code))])
    assert not output.exists()


@pytest.mark.parametrize(
    "bad",
    [
        b"[\xff\xff\xff\x7f",
        b"r\0\0\0\0",
        b"\xdb\x01\0\0\0r\0\0\0\0",
        b"?",
        marshal.dumps(None) + b"trailing",
    ],
)
def test_hostile_marshal_is_rejected(bad: bytes) -> None:
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).unmarshal(bad, "module", code=True)


@pytest.mark.parametrize("transform", [lambda b: b[:-1], lambda b: b + b"hidden", lambda b: b"bad"])
def test_bad_or_trailing_compressed_stream_is_rejected(transform: Callable[[bytes], bytes]) -> None:
    with pytest.raises((native.InvalidArtifact, zlib.error)):
        native.Scanner([]).inflate(transform(zlib.compress(b"safe")), 100)


def test_bombs_counts_depth_and_budget_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).inflate(zlib.compress(b"x" * 1000), 10)
    monkeypatch.setattr(native, "MAX_EXPANDED", 5)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).charge(6)
    monkeypatch.setattr(native, "MAX_ENTRIES", 0)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).entry()
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).unmarshal(b")\x01" * 66 + b"N", "code", code=True)


@pytest.mark.parametrize(
    "extras",
    [
        [("same", b"x", b"a"), ("same", b"x", b"b")],
        [("alias", b"n", b"../escape\0")],
        [("a", b"n", b"b\0"), ("b", b"n", b"a\0")],
        [("bad", b"?", b"x")],
    ],
)
def test_bad_native_entries_fail_closed(extras: list[tuple[str, bytes, bytes]]) -> None:
    with pytest.raises(native.InvalidArtifact):
        scan([("PYZ.pyz", b"z", pyz(module())), *extras])


@pytest.mark.parametrize(
    "result,rule",
    [
        (subprocess.CompletedProcess([], 1, b"", b"sensitive"), "artifact-worker-failed"),
        (subprocess.CompletedProcess([], 0, b"not-json", b""), "artifact-worker-failed"),
        (
            subprocess.CompletedProcess([], 0, b'[["secret\\n", "artifact-invalid"]]', b""),
            "artifact-worker-failed",
        ),
    ],
)
def test_worker_failures_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[bytes],
    rule: str,
) -> None:
    artifact = tmp_path / "fixture.zip"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
    assert native.inspect_artifact(artifact, [TERM]) == [("", rule)]


def test_worker_timeout_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "fixture.zip"
    artifact.write_bytes(b"x")

    def timeout(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired("worker", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert native.inspect_artifact(artifact, []) == [("", "artifact-worker-timeout")]


def test_cli_audits_actual_worker_and_redacts_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    for name in ("LICENSE", "NOTICE", "README.md"):
        (root / name).write_text("safe")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(TERM)
    artifact = tmp_path / "open-brain-0.1.0-linux-x86_64.tar.gz"
    artifact.write_bytes(
        archive(
            executable(
                package([("PYZ.pyz", b"z", pyz(module(f"value = {TERM!r}")))]), "linux-x86_64"
            )
        )
    )
    assert main(["--root", str(root), "--private-denylist", str(denylist)]) == 0
    assert (
        main(
            ["--root", str(root), "--private-denylist", str(denylist), "--artifacts", str(artifact)]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "private-denylist-term" in output and TERM not in output
    assert "artifact-invalid" not in output


@pytest.mark.parametrize(
    "name", ["project.storage.sqlite", "_sysconfigdata__linux_x86_64-linux-gnu"]
)
def test_namespace_and_sqlite_module_identity_are_not_database_files(name: str) -> None:
    value = pyz(module(), name)
    offset = struct.unpack_from("!I", value, 8)[0]
    toc = marshal.loads(value[offset:])
    value = value[:offset] + marshal.dumps([*toc, ("namespace", (3, offset, 0))])
    assert not scan([("PYZ.pyz", b"z", value)]).findings


@pytest.mark.parametrize(
    "field,value", [(1, 1), (2, 0xFFFFFFFF), (3, 0xFFFFFFFF), (4, 2), (5, b"?")]
)
def test_carchive_toc_span_and_flags_rejected(field: int, value: object) -> None:
    pkg = bytearray(package([("PYZ.pyz", b"z", pyz(module()))]))
    cookie = native.COOKIE.unpack_from(pkg, len(pkg) - native.COOKIE.size)
    offset = cookie[2]
    row = list(native.ENTRY.unpack_from(pkg, offset))
    row[field] = value
    native.ENTRY.pack_into(pkg, offset, *row)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).native(
            executable(bytes(pkg), "linux-x86_64"), "linux-x86_64", "native", 0
        )


def test_elf_payload_start_must_match_cookie() -> None:
    binary = bytearray(executable(package([("PYZ.pyz", b"z", pyz(module()))]), "linux-x86_64"))
    offset = struct.unpack_from("<Q", binary, 40)[0] + 64
    start, size = struct.unpack_from("<QQ", binary, offset + 24)
    struct.pack_into("<QQ", binary, offset + 24, start + 1, size - 1)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).native(bytes(binary), "linux-x86_64", "native", 0)


def test_zip_trailing_deflate_bytes_are_rejected() -> None:
    payload = b"safe"
    packed = zlib.compress(payload)[2:-4] + b"hidden"
    name = b"file.txt"
    crc = zlib.crc32(payload)
    local = (
        struct.pack(
            "<4s5H3I2H", b"PK\x03\x04", 20, 0, 8, 0, 0, crc, len(packed), len(payload), len(name), 0
        )
        + name
        + packed
    )
    central = (
        struct.pack(
            "<4s6H3I5H2I",
            b"PK\x01\x02",
            20,
            20,
            0,
            8,
            0,
            0,
            crc,
            len(packed),
            len(payload),
            len(name),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + name
    )
    data = (
        local
        + central
        + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(local), 0)
    )
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).zip(data, "zip", 0)


def test_zip_directory_limit_precedes_zipfile_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    data = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 5000, 5000, 0, 0, 0)

    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("ZipFile allocated before count validation")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).zip(data, "zip", 0)


@pytest.mark.parametrize(
    "name", [TERM + ".zip", "bad\nname.zip", "/" + "/".join(["Users", "example", "file.zip"])]
)
def test_artifact_names_with_matches_or_controls_are_opaque(name: str) -> None:
    assert native.safe_name(name, [TERM], "redacted") == "redacted"


def test_input_limit_checked_before_starting_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.tar"
    path.write_bytes(bytes(20))
    monkeypatch.setattr(native, "MAX_INPUT", 10)
    assert native.inspect_artifact(path, []) == [("", "artifact-limit-exceeded")]


def test_tar_metadata_is_bounded_before_tarfile_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    info = tarfile.TarInfo("metadata")
    info.type = tarfile.XHDTYPE
    info.size = native.MAX_HEADER + 1
    data = info.tobuf() + bytes(((info.size + 511) // 512) * 512 + 1024)

    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("tarfile allocated before metadata validation")

    monkeypatch.setattr(tarfile, "open", unexpected)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).archive(data, "fixture.tar")


def test_many_pax_records_fail_before_tarfile(monkeypatch: pytest.MonkeyPatch) -> None:
    info = tarfile.TarInfo("metadata")
    info.type = tarfile.XHDTYPE
    payload = b"6 a=b\n" * 20
    info.size = len(payload)
    data = info.tobuf() + payload + bytes((-len(payload)) % 512 + 1024)
    monkeypatch.setattr(native, "MAX_ENTRIES", 10)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).tar_preflight(data, native=False)


def test_small_generic_pax_archive_remains_supported() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("safe.txt")
        info.pax_headers = {"comment": "synthetic"}
        info.size = 4
        tar.addfile(info, io.BytesIO(b"safe"))
    scanner = native.Scanner([])
    scanner.archive(buffer.getvalue(), "fixture.tar")
    assert not scanner.findings


@pytest.mark.parametrize("directory", [False, True])
def test_zip_compressed_gaps_are_rejected(directory: bool) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        archive_file.writestr("folder/" if directory else "safe.txt", b"" if directory else b"safe")
    data = buffer.getvalue()
    end = data.rfind(b"PK\x05\x06")
    central = struct.unpack_from("<I", data, end + 16)[0]
    gap = zlib.compress(TERM.encode())
    changed = bytearray(data[:central] + gap + data[central:])
    struct.pack_into("<I", changed, end + len(gap) + 16, central + len(gap))
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([TERM]).zip(bytes(changed), "zip", 0)


def test_tar_hidden_padding_is_rejected() -> None:
    info = tarfile.TarInfo("safe.txt")
    info.size = 1
    padding = zlib.compress(TERM.encode())
    data = info.tobuf() + b"x" + padding + bytes(511 - len(padding) + 1024)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([TERM]).archive(data, "fixture.tar")


def test_native_filename_requires_gzip_encoding() -> None:
    compressed = archive(executable(package([("PYZ.pyz", b"z", pyz(module()))]), "linux-x86_64"))
    plain = zlib.decompress(compressed, 31)
    with pytest.raises(native.InvalidArtifact):
        native.Scanner([]).archive(plain, "open-brain-0.1.0-linux-x86_64.tar.gz")


def reviewed_fixture(monkeypatch: pytest.MonkeyPatch, code: bytes, name: str = "ipaddress") -> None:
    # Synthetic policy keys exercise the real hash comparison without shipping upstream binaries.
    monkeypatch.setattr(
        native, "REVIEWED_PRIVATE_IP_MODULES", frozenset({(name, sha256(code).hexdigest())})
    )


def address_module(extra: str = "") -> bytes:
    address = " " + ".".join(["192", "168", "5", "8"]) + " "
    return module(f"address = {address!r}\n{extra}")


def test_reviewed_policy_contains_only_the_approved_payloads() -> None:
    assert (
        frozenset(
            {
                ("ipaddress", "57a9a0e800670f6f7f44b51a5c1a3ccaa6e159d8d268c0db939ad096917d2f42"),
                (
                    "urllib.request",
                    "30e71da25ad6fa4f4eb5ceefff79e87c157105cfeed0527247cdee657c061188",
                ),
                (
                    "urllib.request",
                    "567e733eef044092e919566a3afd9c9a14b7f1d80c8d07e232a5bacde8a994cc",
                ),
            }
        )
        == native.REVIEWED_PRIVATE_IP_MODULES
    )


@pytest.mark.parametrize("name", ["ipaddress", "urllib.request"])
def test_reviewed_private_ip_requires_exact_name_and_payload(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    code = address_module()
    assert any(
        r == "private-ip-address" for _, r in scan([("PYZ.pyz", b"z", pyz(code, name))]).findings
    )
    reviewed_fixture(monkeypatch, code, name)
    assert not scan([("PYZ.pyz", b"z", pyz(code, name))]).findings
    for changed, identity in [(address_module("extra = 1"), name), (code, "other." + name)]:
        assert any(
            r == "private-ip-address"
            for _, r in scan([("PYZ.pyz", b"z", pyz(changed, identity))]).findings
        )


@pytest.mark.parametrize(
    "value,rule",
    [
        (TERM, "private-denylist-term"),
        ("/" + "/".join(["Users", "synthetic", "file"]), "absolute-home-path"),
        ("api_" + "key=" + "synthetic_value", "credential-assignment"),
    ],
)
def test_reviewed_module_retains_other_rules(
    monkeypatch: pytest.MonkeyPatch, value: str, rule: str
) -> None:
    code = address_module(f"other = {value!r}")
    reviewed_fixture(monkeypatch, code)
    scanner = scan([("PYZ.pyz", b"z", pyz(code, "ipaddress"))])
    assert {r for _, r in scanner.findings} == {rule}


@pytest.mark.parametrize("surface", ["loader", "member", "other-module", "toc"])
def test_reviewed_module_cannot_suppress_other_locations(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    code = address_module()
    reviewed_fixture(monkeypatch, code)
    address = ".".join(["192", "168", "5", "8"]).encode()
    data = pyz(code, "ipaddress")
    if surface == "toc":
        offset = struct.unpack_from("!I", data, 8)[0]
        # Scan a marshal string before rejecting its use in an integer slot.
        data = data[:offset] + marshal.dumps([("ipaddress", (0, 17, address.decode()))])
        scanner = native.Scanner([])
        with pytest.raises(native.InvalidArtifact):
            scanner.pyz(data, "native", 0)
        assert ("native/toc", "private-ip-address") in scanner.findings
        return
    if surface == "other-module":
        offset = struct.unpack_from("!I", data, 8)[0]
        packed = zlib.compress(code)
        toc = [("ipaddress", (0, 17, offset - 17)), ("fixture", (0, offset, len(packed)))]
        data = (
            data[:8]
            + struct.pack("!I", offset + len(packed))
            + data[12:offset]
            + packed
            + marshal.dumps(toc)
        )
    entries = [("PYZ.pyz", b"z", data)]
    if surface == "member":
        entries.append(("data.txt", b"x", address))
    scanner = scan(entries, prefix=b"\0" + address + b"\0" if surface == "loader" else b"")
    expected = {
        "loader": {"archive", "native"},
        "member": {"native/pkg-1"},
        "other-module": {"native/pkg-0/pyz-1"},
    }
    assert {loc for loc, r in scanner.findings if r == "private-ip-address"} == expected[surface]


@pytest.mark.parametrize("failure", ["marshal", "trailing", "objects", "expanded", "findings"])
def test_reviewed_payload_still_requires_complete_bounded_validation(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    code = address_module()
    if failure == "marshal":
        code = marshal.dumps(".".join(["192", "168", "5", "8"]))
    if failure == "trailing":
        code += b"trailing"
    reviewed_fixture(monkeypatch, code)
    if failure == "objects":
        monkeypatch.setattr(native, "MAX_OBJECTS", 10)
    if failure == "expanded":
        monkeypatch.setattr(native, "MAX_EXPANDED", 10)
    if failure == "findings":
        monkeypatch.setattr(native, "MAX_FINDINGS", 1)
    scanner = native.Scanner([])
    with pytest.raises(native.InvalidArtifact):
        scanner.pyz(pyz(code, "ipaddress"), "native", 0)
    if failure != "objects":
        assert scanner.findings


def test_reviewed_payload_is_not_exempt_outside_pyz(monkeypatch: pytest.MonkeyPatch) -> None:
    code = address_module()
    reviewed_fixture(monkeypatch, code)
    assert "private-ip-address" in content_rule_ids(code, ())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive_file:
        archive_file.writestr("ipaddress", code)
    scanner = native.Scanner([])
    scanner.zip(buffer.getvalue(), "wheel", 0)
    assert ("ipaddress", "private-ip-address") in scanner.findings
