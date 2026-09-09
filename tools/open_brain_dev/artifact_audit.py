"""Bounded, non-executing inspection of release archives and PyInstaller 6 bundles.

Untrusted decompression and marshal decoding happen only in a resource-limited
worker. This module intentionally does not import PyInstaller or bundled modules.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
import zlib
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import NoReturn

MAX_INPUT = 64 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_OBJECTS = 500_000
MAX_DEPTH = 64
MAX_ARCHIVE_DEPTH = 4
MAX_MARSHAL = 8 * 1024 * 1024
MAX_TOC = 1024 * 1024
MAX_FINDINGS = 128
MAX_HEADER = 64 * 1024
WORKER_TIMEOUT = 45
NATIVE_NAME = re.compile(
    r"open-brain-[0-9A-Za-z][0-9A-Za-z._-]*-(linux-x86_64|macos-arm64)\.tar\.gz"
)
COOKIE = struct.Struct("!8sIIII64s")
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
ENTRY = struct.Struct("!IIIIBc")

# Owner-approved 2026-09-09; see docs/audits/2026-09-09-ob1-stdlib-content-policy-proposal.md.
# Exact expanded marshal payloads only. New hashes require separate review and approval.
REVIEWED_PRIVATE_IP_MODULES = frozenset(
    {
        ("ipaddress", "57a9a0e800670f6f7f44b51a5c1a3ccaa6e159d8d268c0db939ad096917d2f42"),
        ("urllib.request", "30e71da25ad6fa4f4eb5ceefff79e87c157105cfeed0527247cdee657c061188"),
    }
)


class InvalidArtifact(ValueError):
    """An untrusted format or resource bound failed."""


def require(condition: bool) -> None:
    if not condition:
        raise InvalidArtifact


def safe_name(name: str, terms: Sequence[str], fallback: str) -> str:
    from tools.open_brain_dev.release_audit import content_rule_ids

    folded = unicodedata.normalize("NFKC", name).casefold()
    if (
        re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", name)
        and not any(t in folded for t in terms)
        and not content_rule_ids(name.encode(), ())
    ):
        return name
    return fallback


def inspect_artifact(path: Path, terms: Sequence[str]) -> list[tuple[str, str]]:
    """Return only bounded redacted findings; worker errors never become a pass."""
    header = json.dumps({"name": path.name, "terms": list(terms)}).encode() + b"\n"
    if len(header) > MAX_HEADER:
        return [("", "artifact-limit-exceeded")]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_INPUT:
                return [("", "artifact-limit-exceeded")]
            data = stream.read(MAX_INPUT + 1)
            if len(data) != info.st_size:
                return [("", "artifact-changed-during-read")]
        result = subprocess.run(
            [sys.executable, "-m", "tools.open_brain_dev.artifact_audit"],
            input=header + data,
            capture_output=True,
            timeout=WORKER_TIMEOUT,
            cwd=Path(__file__).resolve().parents[2],
            start_new_session=True,
        )
        if result.returncode or len(result.stdout) > MAX_HEADER:
            return [("", "artifact-worker-failed")]
        values = json.loads(result.stdout)
        require(isinstance(values, list) and len(values) <= MAX_FINDINGS)
        output: list[tuple[str, str]] = []
        for row in values:
            require(isinstance(row, list) and len(row) == 2)
            location, rule = row
            require(isinstance(location, str) and isinstance(rule, str))
            require(
                len(location) <= 512 and re.fullmatch(r"[A-Za-z0-9_./:-]*", location) is not None
            )
            require(rule in RULES)
            output.append((location, rule))
        return output
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker; the parser never spawns children.
        return [("", "artifact-worker-timeout")]
    except OSError, ValueError, AttributeError:
        return [("", "artifact-worker-failed")]


RULES = frozenset(
    {
        "unsafe-archive-path",
        "forbidden-path-family",
        "forbidden-file-type",
        "absolute-home-path",
        "private-ip-address",
        "credential-assignment",
        "content-scan-limit-exceeded",
        "private-denylist-term",
        "symlink-not-allowed",
        "artifact-invalid",
        "artifact-limit-exceeded",
    }
)


_CODE = object()
_ATOM = object()
_NULL = object()
_PENDING = object()


class MarshalReader:
    """Read the pinned Python 3.14 marshal format as data, never as CodeType objects."""

    def __init__(self, data: bytes, scanner: Scanner, location: str, code: bool) -> None:
        require(len(data) <= MAX_MARSHAL)
        self.data = data
        self.scanner = scanner
        self.location = location
        self.allow_code = code
        self.position = 0
        self.references: list[object] = []

    def take(self, size: int) -> bytes:
        require(0 <= size <= len(self.data) - self.position)
        start = self.position
        self.position += size
        return self.data[start : self.position]

    def integer(self) -> int:
        return int.from_bytes(self.take(4), "little", signed=True)

    def read(self, depth: int = 0) -> object:
        self.scanner.objects += 1
        if depth > MAX_DEPTH or self.scanner.objects > MAX_OBJECTS:
            self.scanner.fail_limit()
        marker = self.take(1)[0]
        reference = None
        if marker & 0x80:
            reference = len(self.references)
            self.references.append(_PENDING)
        kind = chr(marker & 0x7F)
        value: object
        if kind == "r":
            index = self.integer()
            require(reference is None and 0 <= index < len(self.references))
            value = self.references[index]
            require(value is not _PENDING)
        elif kind in "NFT.S0":
            value = {"N": None, "F": False, "T": True, ".": Ellipsis, "S": _ATOM, "0": _NULL}[kind]
        elif kind == "i":
            value = self.integer()
        elif kind == "I":
            value = int.from_bytes(self.take(8), "little", signed=True)
        elif kind == "l":
            count = abs(self.integer())
            self.take(count * 2)
            value = _ATOM
        elif kind in "gy":
            self.take(8 if kind == "g" else 16)
            value = _ATOM
        elif kind in "fx":
            self.take(self.take(1)[0])
            if kind == "x":
                self.take(self.take(1)[0])
            value = _ATOM
        elif kind in "stuaAzZ":
            size = self.take(1)[0] if kind in "zZ" else self.integer()
            raw = self.take(size)
            self.scanner.charge(len(raw))
            if kind == "s":
                value = raw
            else:
                value = raw.decode("ascii" if kind in "aAzZ" else "utf-8", errors="strict")
            self.scanner.content(self.location, raw, native=True)
        elif kind in "([)<>":
            count = self.take(1)[0] if kind == ")" else self.integer()
            require(0 <= count <= MAX_OBJECTS - self.scanner.objects)
            items = [self.read(depth + 1) for _ in range(count)]
            require(all(item is not _NULL for item in items))
            value = items if kind == "[" else tuple(items)
        elif kind == "{":
            # Preserve scanning of every pair; never let dictionary materialization erase entries.
            while True:
                key = self.read(depth + 1)
                if key is _NULL:
                    break
                require(self.read(depth + 1) is not _NULL)
            value = _ATOM
        elif kind == ":":
            for _ in range(3):
                require(self.read(depth + 1) is not _NULL)
            value = _ATOM
        elif kind == "c":
            require(self.allow_code)
            integers = [self.integer() for _ in range(5)]
            require(all(n >= 0 for n in integers))
            fields = [self.read(depth + 1) for _ in range(8)]
            require(isinstance(fields[0], bytes) and isinstance(fields[1], tuple))
            require(isinstance(fields[2], tuple) and isinstance(fields[3], tuple))
            assert isinstance(fields[2], tuple) and isinstance(fields[3], tuple)
            require(isinstance(fields[4], bytes) and len(fields[3]) == len(fields[4]))
            require(all(isinstance(n, str) for n in (*fields[2], *fields[3], *fields[5:])))
            require(self.integer() >= 0)
            require(isinstance(self.read(depth + 1), bytes))
            require(isinstance(self.read(depth + 1), bytes))
            value = _CODE
        else:
            raise InvalidArtifact
        if reference is not None:
            self.references[reference] = value
        return value


class Scanner:
    def __init__(self, terms: Sequence[str]) -> None:
        self.terms = terms
        self.expanded = 0
        self.entries = 0
        self.objects = 0
        self.findings: set[tuple[str, str]] = set()

    def fail_limit(self) -> NoReturn:
        self.findings.add(("", "artifact-limit-exceeded"))
        raise InvalidArtifact

    def charge(self, size: int) -> None:
        if size < 0 or size > MAX_MEMBER or self.expanded + size > MAX_EXPANDED:
            self.fail_limit()
        self.expanded += size

    def entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            self.fail_limit()

    def add(self, location: str, rule: str) -> None:
        if len(self.findings) >= MAX_FINDINGS - 1:
            self.fail_limit()
        self.findings.add((location, rule))

    def content(self, location: str, data: bytes, *, native: bool = False) -> None:
        from tools.open_brain_dev.release_audit import TEXT_SCAN_LIMIT, _content_rules

        for finding in _content_rules(
            location,
            data,
            self.terms,
            limit=MAX_MEMBER if native else TEXT_SCAN_LIMIT,
        ):
            self.add(location, finding.rule)

    def name(self, name: str, location: str) -> None:
        from tools.open_brain_dev.release_audit import _path_rules

        require(isinstance(name, str) and len(name) <= 1024 and "\0" not in name)
        for finding in _path_rules(name):
            self.add(location, finding.rule)
        self.content(location, name.encode("utf-8", errors="surrogatepass"))

    def unique(self, name: str, names: set[str], *, aliases: bool = True) -> None:
        require("\\" not in name and "\0" not in name)
        normalized = (
            unicodedata.normalize("NFKC", str(PurePosixPath(name))).casefold() if aliases else name
        )
        require(normalized not in names)
        names.add(normalized)

    def inflate(self, data: bytes, limit: int, *, gzip: bool = False, raw: bool = False) -> bytes:
        require(0 <= limit <= MAX_MEMBER)
        decoder = zlib.decompressobj(31 if gzip else -15 if raw else 15)
        output = decoder.decompress(data, min(limit, MAX_EXPANDED - self.expanded) + 1)
        self.charge(len(output))
        require(len(output) <= limit and decoder.eof)
        require(not decoder.unused_data and not decoder.unconsumed_tail)
        return output

    def unmarshal(self, data: bytes, location: str, *, code: bool) -> object:
        reader = MarshalReader(data, self, location, code)
        value = reader.read()
        require(reader.position == len(data))
        return value

    def code(self, data: bytes, location: str, *, pyc: bool = False) -> None:
        self.content(location, data, native=True)
        if pyc:
            require(len(data) >= 16 and data[:4] == importlib.util.MAGIC_NUMBER)
            require(int.from_bytes(data[4:8], "little") in (0, 1, 3))
            data = data[16:]
        code = self.unmarshal(data, location, code=True)
        require(code is _CODE)

    def zip(self, data: bytes, location: str, depth: int, *, library: bool = False) -> None:
        require(depth <= MAX_ARCHIVE_DEPTH)
        self.content(location or "archive", data, native=True)
        # Bound central-directory allocation before ZipFile constructs ZipInfo objects.
        end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
        require(end >= 0 and end + 22 <= len(data))
        _, disk, central_disk, local_count, count, size, start, comment = struct.unpack_from(
            "<4s4H2IH", data, end
        )
        require(disk == central_disk == 0 and local_count == count)
        require(count <= MAX_ENTRIES - self.entries and size <= MAX_TOC)
        require(start + size == end and end + 22 + comment == len(data))
        position = start
        for _ in range(count):
            require(position + 46 <= end and data[position : position + 4] == b"PK\x01\x02")
            name_size, extra_size, comment_size = struct.unpack_from("<HHH", data, position + 28)
            require(0 < name_size <= 1024)
            position += 46 + name_size + extra_size + comment_size
        require(position == end)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(len(archive.infolist()) == count and archive.start_dir == start)
            names: set[str] = set()
            spans: list[tuple[int, int]] = []
            for index, member in enumerate(archive.infolist()):
                self.entry()
                loc = (
                    f"{location}/zip-{index}"
                    if library
                    else safe_name(
                        member.filename,
                        self.terms,
                        f"member-{index}",
                    )
                )
                self.unique(member.filename, names, aliases=library)
                self.name(member.filename, loc)
                require(not member.flag_bits & 1)
                mode = (member.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    self.add(loc, "symlink-not-allowed")
                    continue
                require(mode in (0, stat.S_IFREG, stat.S_IFDIR))
                if member.is_dir():
                    require(not library and member.file_size == 0)
                limit = MAX_MARSHAL if library else 2 * 1024 * 1024
                if member.file_size > limit:
                    self.add(loc, "content-scan-limit-exceeded")
                    continue
                require(0 <= member.header_offset <= len(data) - 30)
                header = struct.unpack_from("<4s5H3I2H", data, member.header_offset)
                magic, _, flags, method, _, _, crc, packed, unpacked, name_size, extra = header
                require(magic == b"PK\x03\x04" and flags == member.flag_bits)
                require(flags & ~0x800 == 0 and method in (0, 8))
                require(method == member.compress_type and crc == member.CRC)
                require(packed == member.compress_size and unpacked == member.file_size)
                left = member.header_offset + 30
                right = left + name_size
                require(right + extra + packed <= archive.start_dir)
                require(
                    data[left:right].decode("utf-8" if flags & 0x800 else "cp437")
                    == member.filename
                )
                left = right + extra
                compressed = data[left : left + packed]
                if method == 8:
                    payload = self.inflate(compressed, member.file_size, raw=True)
                else:
                    self.charge(len(compressed))
                    payload = compressed
                require(len(payload) == member.file_size and zlib.crc32(payload) == member.CRC)
                spans.append((member.header_offset, left + packed))
                if member.is_dir():
                    continue
                if library:
                    require(member.filename.endswith(".pyc"))
                    self.code(payload, loc, pyc=True)
                else:
                    self.content(loc, payload)
            self.contiguous(spans, 0, start)

    def pyz(self, data: bytes, location: str, depth: int) -> None:
        require(depth <= MAX_ARCHIVE_DEPTH and len(data) >= 12 and data[:4] == b"PYZ\0")
        require(data[4:8] == importlib.util.MAGIC_NUMBER)
        offset = struct.unpack_from("!I", data, 8)[0]
        require(12 <= offset <= len(data) and len(data) - offset <= MAX_TOC)
        toc = self.unmarshal(data[offset:], location + "/toc", code=False)
        # Pinned PyInstaller writes a list, preserving duplicates for validation.
        require(isinstance(toc, list))
        assert isinstance(toc, list)
        names: set[str] = set()
        spans: list[tuple[int, int]] = []
        for index, row in enumerate(toc):
            self.entry()
            require(isinstance(row, tuple) and len(row) == 2)
            name, entry = row
            require(isinstance(name, str) and isinstance(entry, tuple) and len(entry) == 3)
            kind, start, size = entry
            require(all(type(n) is int for n in entry))
            require(kind in (0, 1, 3) and size >= 0)
            loc = f"{location}/pyz-{index}"
            self.unique(name, names)
            require(all(part.replace("-", "_").isidentifier() for part in name.split(".")))
            self.content(loc, name.encode())
            # PYZ keys are import identities, not filenames with a suffix such as .sqlite.
            self.name(name.replace(".", "/") + ".py", loc)
            if kind == 3:
                require(size == 0 and 17 <= start <= offset)
                continue
            require(size > 0 and start >= 17 and start + size <= offset)
            spans.append((start, start + size))
            expanded = self.inflate(data[start : start + size], MAX_MARSHAL)
            self.code(expanded, loc)
            # Full parsing/scanning must succeed first; all other rules and locations survive.
            if (name, sha256(expanded).hexdigest()) in REVIEWED_PRIVATE_IP_MODULES:
                self.findings.discard((loc, "private-ip-address"))
        require(data[12:17] == bytes(5))
        self.contiguous(spans, 17, offset)

    @staticmethod
    def contiguous(spans: list[tuple[int, int]], start: int, end: int) -> None:
        for left, right in sorted(spans):
            require(left == start and right > left)
            start = right
        require(start == end)

    @staticmethod
    def native_bounds(data: bytes, platform: str) -> tuple[int | None, int]:
        if platform == "linux-x86_64":
            require(len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01")
            require(int.from_bytes(data[18:20], "little") == 62)
            offset = struct.unpack_from("<Q", data, 40)[0]
            width, count, strings = struct.unpack_from("<HHH", data, 58)
            require(width == 64 and 0 < count <= 256 and strings < count)
            require(offset >= 64 and offset + count * width == len(data))
            rows = [
                struct.unpack_from("<IIQQQQIIQQ", data, offset + i * width) for i in range(count)
            ]
            string_table = rows[strings]
            start, size = string_table[4:6]
            require(start + size <= offset)
            names = data[start : start + size]
            packages = []
            for row in rows:
                require(row[0] < len(names))
                name = names[row[0] :].split(b"\0", 1)[0]
                if name == b"pydata":
                    require(row[1] == 1 and row[4] >= 64 and row[4] + row[5] <= start)
                    packages.append((int(row[4]), int(row[4] + row[5])))
            require(len(packages) == 1)
            return packages[0]
        require(len(data) >= 32 and data[:4] == b"\xcf\xfa\xed\xfe")
        require(struct.unpack_from("<I", data, 4)[0] == 0x100000C)
        count, size = struct.unpack_from("<II", data, 16)
        require(count <= 256 and size <= len(data) - 32)
        position = 32
        signature: tuple[int, int] | None = None
        for _ in range(count):
            require(position + 8 <= 32 + size)
            command, length = struct.unpack_from("<II", data, position)
            require(length >= 8 and position + length <= 32 + size)
            if command == 0x1D:
                require(length == 16 and signature is None)
                signature = struct.unpack_from("<II", data, position + 8)
            position += length
        require(position == 32 + size and signature is not None)
        assert signature is not None
        start, length = signature
        require(start >= position and length > 0 and start + length == len(data))
        return None, start

    def native(self, data: bytes, platform: str, location: str, depth: int) -> None:
        require(depth <= MAX_ARCHIVE_DEPTH)
        self.content(location, data, native=True)
        package_start, end = self.native_bounds(data, platform)
        cookie = data.rfind(COOKIE_MAGIC, max(0, end - COOKIE.size - 15), end)
        require(cookie >= 64 and cookie + COOKIE.size <= end)
        require(not any(data[cookie + COOKIE.size : end]))
        magic, size, toc_offset, toc_size, version, library = COOKIE.unpack_from(data, cookie)
        start = cookie + COOKIE.size - size
        require(magic == COOKIE_MAGIC and version == 314 and start >= 32)
        require(package_start is None or package_start == start)
        require(toc_size <= MAX_TOC and toc_offset + toc_size == size - COOKIE.size)
        require(toc_offset >= 0 and b"\0" in library and library.rstrip(b"\0"))
        self.content(location + "/library-name", library)
        toc = data[start + toc_offset : cookie]
        require(len(toc) == toc_size)
        position = 0
        names: set[str] = set()
        spans: list[tuple[int, int]] = []
        links: dict[str, str] = {}
        paths: set[str] = set()
        pyz_count = 0
        while position < len(toc):
            self.entry()
            require(position + ENTRY.size <= len(toc))
            length, offset, stored, expanded, compressed, kind = ENTRY.unpack_from(toc, position)
            require(ENTRY.size < length <= ENTRY.size + 1040 and position + length <= len(toc))
            raw_name = toc[position + ENTRY.size : position + length]
            require(b"\0" in raw_name)
            name_bytes, padding = raw_name.split(b"\0", 1)
            require(not any(padding))
            name = name_bytes.decode("utf-8")
            position += length
            loc = f"{location}/pkg-{len(paths)}"
            require(kind in (b"m", b"M", b"s", b"b", b"x", b"z", b"n", b"o"))
            require(compressed in (0, 1))
            self.name(name, loc)
            if kind == b"o":
                require(0 <= offset <= toc_offset and stored == expanded == compressed == 0)
                continue
            self.unique(name, names)
            require(
                bool(name) and not PurePosixPath(name).is_absolute() and ".." not in name.split("/")
            )
            paths.add(name)
            require(stored > 0 and offset + stored <= toc_offset and expanded <= MAX_MEMBER)
            spans.append((offset, offset + stored))
            payload = data[start + offset : start + offset + stored]
            if compressed:
                payload = self.inflate(payload, expanded)
            else:
                self.charge(len(payload))
            require(len(payload) == expanded)
            if kind == b"z":
                require(name == "PYZ.pyz")
                pyz_count += 1
                self.pyz(payload, loc, depth + 1)
            elif kind in (b"m", b"M", b"s"):
                self.code(payload, loc)
            elif kind == b"n":
                require(payload.endswith(b"\0"))
                target = payload[:-1].decode("utf-8")
                self.content(loc, payload)
                require(bool(target) and "\0" not in target and "\\" not in target)
                require(not PurePosixPath(target).is_absolute())
                joined = list(PurePosixPath(name).parent.parts)
                for part in target.split("/"):
                    if part == "..":
                        require(bool(joined))
                        joined.pop()
                    elif part not in ("", "."):
                        joined.append(part)
                links[name] = "/".join(joined)
            elif name == "base_library.zip":
                self.zip(payload, loc, depth + 1, library=True)
            else:
                self.content(loc, payload, native=kind == b"b")
        require(pyz_count == 1)
        self.contiguous(spans, 0, toc_offset)
        for link, target in links.items():
            visited = {link}
            for _ in range(MAX_ARCHIVE_DEPTH):
                # Framework directory symlinks may occur inside the target path.
                replacement = next(
                    (p for p in links if target == p or target.startswith(p + "/")), None
                )
                if replacement is None:
                    require(target in paths or any(p.startswith(target + "/") for p in paths))
                    break
                require(replacement not in visited)
                visited.add(replacement)
                target = links[replacement] + target[len(replacement) :]
            else:
                raise InvalidArtifact

    def tar_preflight(self, data: bytes, *, native: bool) -> None:
        """Bound hidden extension records before tarfile allocates or recurses over them."""
        position = 0
        metadata = 0
        extensions = 0
        while position + 512 <= len(data) and any(data[position : position + 512]):
            self.entry()
            header = data[position : position + 512]
            raw_size = header[124:136].strip(b"\0 ")
            require(bool(raw_size) and all(c in b"01234567" for c in raw_size))
            size = int(raw_size, 8)
            require(size <= MAX_MEMBER)
            start = position + 512
            position = start + ((size + 511) // 512) * 512
            require(position <= len(data))
            require(not any(data[start + size : position]))
            kind = header[156:157]
            require(kind in (b"\0", b"0", b"1", b"2", b"5", b"x", b"g", b"L", b"K"))
            if kind in (b"x", b"g", b"L", b"K"):
                extensions += 1
                metadata += size
                require(not native and extensions <= MAX_DEPTH and metadata <= MAX_TOC)
                require(size <= MAX_HEADER)
                if kind in (b"L", b"K"):
                    require(size <= 1025)
                    continue
                cursor = start
                while cursor < start + size:
                    self.entry()
                    space = data.find(b" ", cursor, min(cursor + 9, start + size))
                    require(space > cursor and data[cursor:space].isdigit())
                    length = int(data[cursor:space])
                    require(length > space - cursor + 2 and cursor + length <= start + size)
                    record = data[space + 1 : cursor + length]
                    require(record.endswith(b"\n") and b"=" in record)
                    # Sparse extensions can trigger additional unbounded parsing in tarfile.
                    require(not record.startswith(b"GNU.sparse."))
                    cursor += length
            else:
                extensions = 0
        require(position + 1024 <= len(data) and not any(data[position:]))

    def archive(self, data: bytes, filename: str) -> None:
        self.charge(len(data))
        self.content("archive", data, native=True)
        native = NATIVE_NAME.fullmatch(filename)
        require(native is None or data.startswith(b"\x1f\x8b"))
        if data.startswith(b"PK"):
            require(native is None)
            self.zip(data, "", 0)
            return
        if data.startswith(b"\x1f\x8b"):
            data = self.inflate(data, MAX_MEMBER, gzip=True)
            self.content("archive", data, native=True)
        require(len(data) <= MAX_MEMBER)
        self.tar_preflight(data, native=native is not None)
        names: set[str] = set()
        count = 0
        last_end = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                count += 1
                self.unique(member.name, names, aliases=native is not None)
                loc = safe_name(member.name, self.terms, f"member-{count}")
                self.name(member.name, loc)
                for key, value in member.pax_headers.items():
                    self.content(f"member-{count}/metadata", (key + value).encode())
                last_end = member.offset_data + ((member.size + 511) // 512) * 512
                if member.issym() or member.islnk():
                    self.add(loc, "symlink-not-allowed")
                    continue
                if member.isdir():
                    require(native is None)
                    continue
                require(member.isfile() and not member.sparse)
                limit = MAX_MEMBER if native else 2 * 1024 * 1024
                if member.size > limit:
                    self.add(loc, "content-scan-limit-exceeded")
                    continue
                self.charge(member.size)
                stream = archive.extractfile(member)
                require(stream is not None)
                assert stream is not None
                payload = stream.read(member.size + 1)
                require(len(payload) == member.size)
                if native:
                    require(count == 1 and member.name == "open-brain" and member.mode & 0o100 != 0)
                    require(not member.pax_headers)
                    self.native(payload, native.group(1), "native", 1)
                else:
                    self.content(loc, payload)
        require(last_end + 1024 <= len(data) and not any(data[last_end:]))
        if native:
            require(count == 1)


def worker() -> int:
    scanner: Scanner | None = None
    try:
        if sys.platform not in ("darwin", "linux") or sys.version_info[:2] != (3, 14):
            raise InvalidArtifact
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        header = sys.stdin.buffer.readline(MAX_HEADER + 1)
        require(header.endswith(b"\n") and len(header) <= MAX_HEADER)
        request = json.loads(header)
        require(isinstance(request, dict) and set(request) == {"name", "terms"})
        name, terms = request["name"], request["terms"]
        require(isinstance(name, str) and len(name) <= 200)
        require(isinstance(terms, list) and len(terms) <= 1024)
        require(all(isinstance(t, str) and 0 < len(t) <= 4096 for t in terms))
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(0 < len(data) <= MAX_INPUT)
        scanner = Scanner(terms)
        scanner.archive(data, name)
    except Exception:
        if scanner is None:
            print(json.dumps([["", "artifact-invalid"]]))
            return 0
        if ("", "artifact-limit-exceeded") not in scanner.findings:
            scanner.findings.add(("", "artifact-invalid"))
    assert scanner is not None
    print(json.dumps(sorted(scanner.findings), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(worker())
