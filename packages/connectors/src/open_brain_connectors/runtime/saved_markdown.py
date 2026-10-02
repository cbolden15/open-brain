"""Pure, selected-root intake for saved third-party Markdown.

This module deliberately has no configured roots, source URLs, or egress
authority.  The host supplies all of those bindings for one run.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlparse

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = [
    "SAVED_MARKDOWN_NORMALIZATION_VERSION",
    "SavedMarkdownCandidate",
    "SavedMarkdownIdentity",
    "SavedMarkdownLimits",
    "SavedMarkdownRootAdapter",
    "SavedMarkdownScan",
    "normalize_saved_markdown",
]

SAVED_MARKDOWN_NORMALIZATION_VERSION = "saved-markdown-continuous.v1"
_CONNECTOR_NAME = "saved_markdown"
_RESOURCE_TYPE = "markdown_root"
_MAX_DELIVERIES = 25
_MAX_FILE_BYTES = 1_048_576
_MAX_TRANSFORMED_CHARACTERS = 65_536
# ReferencePayload's 65,536-character bound is not a byte ceiling.  This
# lower, explicit ceiling keeps UTF-8 content inside the supported capture
# body's initial bounded contract.
_MAX_CORE_PAYLOAD_BYTES = 65_536
_FRONTMATTER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r"^[=-]{3,}[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class SavedMarkdownLimits:
    """The bounded Phase 2 defaults; a host may only lower these limits."""

    max_deliveries: int = _MAX_DELIVERIES
    max_file_bytes: int = _MAX_FILE_BYTES
    max_transformed_characters: int = _MAX_TRANSFORMED_CHARACTERS
    max_core_payload_bytes: int = _MAX_CORE_PAYLOAD_BYTES

    def __post_init__(self) -> None:
        values = (
            self.max_deliveries,
            self.max_file_bytes,
            self.max_transformed_characters,
            self.max_core_payload_bytes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ConnectorContractError("invalid saved markdown limits")
        if (
            self.max_deliveries > _MAX_DELIVERIES
            or self.max_file_bytes > _MAX_FILE_BYTES
            or self.max_transformed_characters > _MAX_TRANSFORMED_CHARACTERS
            or self.max_core_payload_bytes > _MAX_CORE_PAYLOAD_BYTES
        ):
            raise ConnectorContractError("invalid saved markdown limits")


@dataclass(frozen=True, slots=True)
class SavedMarkdownIdentity:
    """Stable item identity plus immutable version and delivery identities.

    The original Phase 2 identity incorrectly included body hashes in the
    logical external ID.  B keeps that shared connector key stable and binds
    body/policy changes only to the revision and delivery domains.
    """

    destination_identity: str
    accepted_source_identity: str
    relative_item_identity: str
    original_sha256: str
    transformed_sha256: str
    normalization_version: str
    privacy_policy_version: str

    def __post_init__(self) -> None:
        values = (
            self.destination_identity,
            self.accepted_source_identity,
            self.relative_item_identity,
            self.original_sha256,
            self.transformed_sha256,
            self.normalization_version,
            self.privacy_policy_version,
        )
        if any(type(value) is not str or not value for value in values):
            raise ConnectorContractError("invalid saved markdown identity")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (self.original_sha256, self.transformed_sha256)
        ):
            raise ConnectorContractError("invalid saved markdown identity")

    def _digest(self, purpose: str) -> str:
        stable = {
            "accepted_source_identity": self.accepted_source_identity,
            "destination_identity": self.destination_identity,
            "relative_item_identity": self.relative_item_identity,
        }
        version = {
            **stable,
            "normalization_version": self.normalization_version,
            "original_sha256": self.original_sha256,
            "privacy_policy_version": self.privacy_policy_version,
            "transformed_sha256": self.transformed_sha256,
        }
        if purpose == "item":
            value = {"domain": "saved-markdown-item.v1", **stable}
        elif purpose == "revision":
            value = {"domain": "saved-markdown-version.v1", **version}
        elif purpose == "delivery":
            value = {"domain": "saved-markdown-delivery.v1", **version}
        else:
            raise ConnectorContractError("invalid saved markdown identity")
        return sha256(portable_canonical_json_bytes(value)).hexdigest()

    @property
    def delivery_id(self) -> str:
        return "saved-markdown." + self._digest("delivery")

    @property
    def revision_id(self) -> str:
        return self._digest("revision")

    @property
    def item_id(self) -> str:
        return self._digest("item")


@dataclass(frozen=True, slots=True)
class SavedMarkdownCandidate:
    """One dry-run result. Refused candidates never include transformed text."""

    relative_path: str
    identity: SavedMarkdownIdentity | None
    intake: SourceRecordIntake | None
    refusal_code: str | None

    def __post_init__(self) -> None:
        if (
            type(self.relative_path) is not str
            or not self.relative_path
            or self.relative_path.startswith("/")
            or ".." in Path(self.relative_path).parts
            or (self.intake is None) == (self.refusal_code is None)
            or (self.intake is not None and self.identity is None)
            or (self.refusal_code is not None and self.identity is not None)
        ):
            raise ConnectorContractError("invalid saved markdown candidate")

    @property
    def accepted(self) -> bool:
        return self.intake is not None


@dataclass(frozen=True, slots=True)
class SavedMarkdownScan:
    """A deterministic bounded scan. Incomplete scans never mean removals."""

    candidates: tuple[SavedMarkdownCandidate, ...]
    complete: bool
    next_cursor: str | None = None


def normalize_saved_markdown(
    raw: bytes, *, version: str = SAVED_MARKDOWN_NORMALIZATION_VERSION
) -> str:
    """Remove owner frontmatter and a top-level ``Why Saved`` context section."""

    if type(raw) is not bytes or version != SAVED_MARKDOWN_NORMALIZATION_VERSION:
        raise ConnectorContractError("invalid saved markdown")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConnectorContractError("saved_markdown_invalid_utf8") from error
    if "\x00" in text or has_redaction_finding(text):
        raise ConnectorContractError("saved_markdown_secret_bearing")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = _without_frontmatter(lines)
    result = _without_why_saved(lines)
    normalized = "\n".join(result).strip()
    if not normalized:
        raise ConnectorContractError("saved_markdown_empty")
    return normalized + "\n"


class SavedMarkdownRootAdapter:
    """Read only stable regular ``.md`` files under one caller-bound root."""

    def __init__(
        self,
        root: Path,
        *,
        destination_identity: str,
        accepted_source_identity: str,
        source_reference: str,
        privacy: PrivacyDecision,
        limits: SavedMarkdownLimits | None = None,
    ) -> None:
        limits = SavedMarkdownLimits() if limits is None else limits
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or not isinstance(limits, SavedMarkdownLimits)
            or not isinstance(privacy, PrivacyDecision)
            or privacy.authority.cloud
            or privacy.authority.external_egress
            or not _valid_identity(destination_identity)
            or not _valid_identity(accepted_source_identity)
            or not _supported_reference(source_reference)
        ):
            raise ConnectorContractError("invalid saved markdown source")
        self._root = root
        self._destination_identity = destination_identity
        self._accepted_source_identity = accepted_source_identity
        self._source_reference = source_reference.rstrip("/")
        self._privacy = privacy
        self._limits = limits

    @property
    def selection(self) -> SourceResourceSelection:
        return SourceResourceSelection(
            connector_name=_CONNECTOR_NAME,
            connection_id="source:" + sha256(self._accepted_source_identity.encode()).hexdigest(),
            resource_id="destination:" + sha256(self._destination_identity.encode()).hexdigest(),
            resource_type=_RESOURCE_TYPE,
        )

    def dry_run(self, cursor: str | None = None) -> SavedMarkdownScan:
        """Return one bounded inventory page.

        The cursor is an adapter-generated ordinal, not a caller-selected
        filesystem path.  Restarting with it revisits only metadata necessary
        to reach the recorded ordinal and never makes the first 25 stable
        files starve later paths.
        """
        start = _scan_offset(cursor)
        try:
            root_stat = self._root.lstat()
            root = self._root.resolve(strict=True)
        except OSError:
            return SavedMarkdownScan((), False)
        if stat.S_ISLNK(root_stat.st_mode) or not root.is_dir():
            return SavedMarkdownScan((), False)
        inventory: list[SavedMarkdownCandidate] = []
        complete = True
        for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if not _excluded_directory(name))
            for filename in sorted(filenames):
                path = Path(directory) / filename
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    complete = False
                    continue
                if _excluded_file(filename):
                    continue
                if path.suffix.lower() != ".md":
                    inventory.append(_refused(relative, "unsupported_format"))
                    continue
                candidate = self._candidate(root, path, relative)
                inventory.append(candidate)
                complete = complete and candidate.refusal_code not in {
                    "unstable_read",
                    "root_escape",
                }
        if start > len(inventory):
            raise ConnectorContractError("invalid saved markdown page")
        page: list[SavedMarkdownCandidate] = []
        accepted = 0
        index = start
        while index < len(inventory):
            candidate = inventory[index]
            if candidate.accepted and accepted >= self._limits.max_deliveries:
                break
            page.append(candidate)
            accepted += int(candidate.accepted)
            index += 1
        next_cursor = None if index == len(inventory) else f"scan.{index}"
        return SavedMarkdownScan(tuple(page), complete and next_cursor is None, next_cursor)

    def _candidate(self, root: Path, path: Path, relative: str) -> SavedMarkdownCandidate:
        try:
            link_info = path.lstat()
            if stat.S_ISLNK(link_info.st_mode):
                return _refused(relative, "symlink")
            if not stat.S_ISREG(link_info.st_mode):
                return _refused(relative, "unsupported_format")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                return _refused(relative, "root_escape")
            if link_info.st_size > self._limits.max_file_bytes:
                return _refused(relative, "file_too_large")
            before = _fingerprint(link_info)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    raw = handle.read(self._limits.max_file_bytes + 1)
                after = _fingerprint(os.fstat(descriptor))
            finally:
                os.close(descriptor)
            if before != after or len(raw) > self._limits.max_file_bytes:
                return _refused(relative, "unstable_read")
        except OSError:
            return _refused(relative, "unstable_read")
        try:
            transformed = normalize_saved_markdown(raw)
        except ConnectorContractError as error:
            return _refused(relative, str(error))
        if len(transformed) > self._limits.max_transformed_characters:
            return _refused(relative, "transformed_too_large")
        if len(transformed.encode("utf-8")) > self._limits.max_core_payload_bytes:
            return _refused(relative, "core_payload_too_large")
        identity = SavedMarkdownIdentity(
            destination_identity=self._destination_identity,
            accepted_source_identity=self._accepted_source_identity,
            relative_item_identity=_normalized_relative(relative),
            original_sha256=sha256(raw).hexdigest(),
            transformed_sha256=sha256(transformed.encode("utf-8")).hexdigest(),
            normalization_version=SAVED_MARKDOWN_NORMALIZATION_VERSION,
            privacy_policy_version=self._privacy.policy_version,
        )
        key = SourceRecordKey(
            connector_name=_CONNECTOR_NAME,
            connection_id=self.selection.connection_id,
            resource_id=self.selection.resource_id,
            external_id="item:" + identity.item_id,
            revision_id=identity.revision_id,
        )
        title = _title(transformed)
        intake = SourceRecordIntake(
            key=key,
            url=self._source_reference + "/" + quote(relative, safe="/"),
            text=transformed,
            privacy=self._privacy,
            title=title,
        )
        return SavedMarkdownCandidate(relative, identity, intake, None)


def _without_frontmatter(lines: list[str]) -> list[str]:
    if not lines or lines[0] != "---":
        return lines
    for index, line in enumerate(lines[1:], start=1):
        if line in {"---", "..."}:
            for value in lines[1:index]:
                if not value.strip():
                    continue
                key, separator, _value = value.partition(":")
                if not separator or _FRONTMATTER_KEY.fullmatch(key.strip()) is None:
                    raise ConnectorContractError("saved_markdown_malformed_frontmatter")
            return lines[index + 1 :]
    raise ConnectorContractError("saved_markdown_malformed_frontmatter")


def _without_why_saved(lines: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    fence: str | None = None
    while index < len(lines):
        line = lines[index]
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            result.append(line)
            index += 1
            continue
        if fence is None:
            heading = _heading_at(lines, index)
            if heading is not None and heading[0] == "why saved":
                level, consumed = heading[1], heading[2]
                index += consumed
                while index < len(lines):
                    next_heading = _heading_at(lines, index)
                    if next_heading is not None and next_heading[1] <= level:
                        break
                    index += 1
                continue
        result.append(line)
        index += 1
    return result


def _heading_at(lines: list[str], index: int) -> tuple[str, int, int] | None:
    atx = _ATX_HEADING.match(lines[index])
    if atx:
        return (atx.group(2).strip().casefold(), len(atx.group(1)), 1)
    if index + 1 < len(lines) and _SETEXT_UNDERLINE.match(lines[index + 1]):
        level = 1 if lines[index + 1].lstrip().startswith("=") else 2
        return (lines[index].strip().casefold(), level, 2)
    return None


def _title(text: str) -> str:
    for line in text.splitlines():
        match = _ATX_HEADING.match(line)
        if match and match.group(2).strip():
            return match.group(2).strip()[:200]
    return "Saved Markdown"


def _excluded_directory(name: str) -> bool:
    return name.startswith(".") or _temporary_name(name)


def _excluded_file(name: str) -> bool:
    return name.startswith(".") or _temporary_name(name)


def _temporary_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered.endswith(("~", ".tmp", ".swp", ".part"))
        or "sync-conflict" in lowered
        or "conflicted copy" in lowered
    )


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)


def _refused(relative_path: str, code: str) -> SavedMarkdownCandidate:
    return SavedMarkdownCandidate(relative_path, None, None, code)


def _valid_identity(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value))


def _normalized_relative(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in Path(normalized).parts
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ConnectorContractError("invalid saved markdown relative path")
    return normalized


def _scan_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    match = re.fullmatch(r"scan\.([0-9]{1,9})", cursor)
    if match is None:
        raise ConnectorContractError("invalid saved markdown page")
    return int(match.group(1))


def _supported_reference(value: str) -> bool:
    if type(value) is not str or len(value) > 1_000:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not parsed.query
        and not parsed.fragment
    )
