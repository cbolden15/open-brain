"""Bounded local adapter for the separately packaged Graphify Markdown helper."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import ManagedGraphSnapshot

GRAPHIFY_PROTOCOL = "open-brain-graphify-helper-v1"
GRAPHIFY_VERSION = "0.9.57"
GRAPHIFY_UPSTREAM_COMMIT = "3f82bf7f837a07fb0f7668fbdbd5662801906942"
GRAPHIFY_PATCH_ID = "ob1-graphify-markdown-1"
GRAPHIFY_PATCH_SHA256 = "f7417ee080e1f5050f41b525f4bb0f97910c14abf6531dcf38cbd2ff28da796b"
GRAPHIFY_PARSER_PROFILE = "pyyaml-6.0.3-pure-python"
MAX_GRAPHIFY_BYTES = 16 * 1024
MAX_GRAPHIFY_SECONDS = 60
MAX_GRAPHIFY_ITEMS = 64

_PAGE_ID = re.compile(
    r"^page_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_FAILURE_CODES = {
    "adapter_unavailable",
    "ambiguous_snapshot",
    "cancelled",
    "cleanup_failed",
    "component_mismatch",
    "input_invalid",
    "output_invalid",
    "timeout",
    "worker_failed",
}


class GraphifyFailure(RuntimeError):
    """A structural projection failure with one bounded public category."""

    def __init__(self, code: str) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError("invalid Graphify failure")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GraphifyLink:
    source_note_id: str
    target_note_id: str
    kind: str = "explicit_reference"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_note_id, str)
            or not isinstance(self.target_note_id, str)
            or _PAGE_ID.fullmatch(self.source_note_id) is None
            or _PAGE_ID.fullmatch(self.target_note_id) is None
            or self.source_note_id == self.target_note_id
            or self.kind != "explicit_reference"
        ):
            raise ValueError("invalid Graphify link")


@dataclass(frozen=True, slots=True)
class GraphifyDiagnostic:
    code: str
    source_note_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in {
            "ambiguous_snapshot",
            "unresolved_reference",
        } or (
            self.source_note_id is not None
            and (
                not isinstance(self.source_note_id, str)
                or _PAGE_ID.fullmatch(self.source_note_id) is None
            )
        ):
            raise ValueError("invalid Graphify diagnostic")


@dataclass(frozen=True, slots=True)
class GraphifyExtraction:
    snapshot_sha256: str
    status: str
    links: tuple[GraphifyLink, ...]
    diagnostics: tuple[GraphifyDiagnostic, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot_sha256, str)
            or _HEX64.fullmatch(self.snapshot_sha256) is None
            or self.status not in {"ok", "blocked"}
            or not isinstance(self.links, tuple)
            or not all(isinstance(link, GraphifyLink) for link in self.links)
            or not isinstance(self.diagnostics, tuple)
            or not all(
                isinstance(diagnostic, GraphifyDiagnostic)
                for diagnostic in self.diagnostics
            )
            or len(self.links) > MAX_GRAPHIFY_ITEMS
            or len(self.diagnostics) > MAX_GRAPHIFY_ITEMS
            or len(set(self.links)) != len(self.links)
            or len(set(self.diagnostics)) != len(self.diagnostics)
        ):
            raise ValueError("invalid Graphify extraction")


class GraphifyHelperTransport(Protocol):
    def __call__(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        request: bytes,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> bytes: ...


class SubprocessGraphifyTransport:
    """Run one normal-user foreground helper with bounded disk-backed output."""

    def __call__(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        request: bytes,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        cancelled: Callable[[], bool],
    ) -> bytes:
        if cancelled():
            raise GraphifyFailure("cancelled")
        try:
            metadata = executable.stat(follow_symlinks=False)
        except OSError:
            raise GraphifyFailure("adapter_unavailable") from None
        if (
            not executable.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o111 == 0
        ):
            raise GraphifyFailure("adapter_unavailable")
        process: subprocess.Popen[bytes] | None = None
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    (str(executable), *arguments),
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    cwd="/",
                    env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                    close_fds=True,
                )
                deadline = time.monotonic() + timeout_seconds
                pending: bytes | None = request
                while True:
                    if cancelled():
                        raise GraphifyFailure("cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GraphifyFailure("timeout")
                    try:
                        process.communicate(pending, timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pending = None
                        continue
                    break
                if process.returncode != 0:
                    raise GraphifyFailure("worker_failed")
                if output.tell() > max_output_bytes:
                    raise GraphifyFailure("output_invalid")
                output.seek(0)
                return output.read(max_output_bytes + 1)
            except GraphifyFailure:
                raise
            except (OSError, subprocess.SubprocessError):
                raise GraphifyFailure("worker_failed") from None
            finally:
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            process.kill()
                            process.wait(timeout=0.5)
                        except (OSError, subprocess.TimeoutExpired):
                            raise GraphifyFailure("cleanup_failed") from None


class GraphifyAdapter:
    """Validate the pinned helper identity and extract one accepted snapshot."""

    def __init__(
        self,
        executable: Path,
        *,
        transport: GraphifyHelperTransport | None = None,
    ) -> None:
        if not isinstance(executable, Path) or not executable.is_absolute():
            raise ValueError("invalid Graphify executable")
        self.executable = executable
        self._transport = SubprocessGraphifyTransport() if transport is None else transport
        if not callable(self._transport):
            raise ValueError("invalid Graphify transport")

    @property
    def identity(self) -> str:
        return "graphify:" + sha256(
            portable_canonical_json_bytes(_component_identity())
        ).hexdigest()

    def extract(
        self,
        snapshot: ManagedGraphSnapshot,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> GraphifyExtraction:
        if not isinstance(snapshot, ManagedGraphSnapshot) or not callable(cancelled):
            raise ValueError("invalid Graphify extraction request")
        capabilities = self._exchange(("--capabilities",), b"", cancelled=cancelled)
        if capabilities != _capabilities():
            raise GraphifyFailure("component_mismatch")
        request = portable_canonical_json_bytes(
            {
                "notes": [
                    {
                        "body": source.body,
                        "id": source.note_id,
                        "path": source.relative_path,
                    }
                    for source in snapshot.sources
                ],
                "operation": "extract_markdown",
                "protocol": GRAPHIFY_PROTOCOL,
            }
        )
        if not snapshot.sources or len(request) > MAX_GRAPHIFY_BYTES:
            raise GraphifyFailure("input_invalid")
        response = self._exchange(
            ("--extract-markdown",), request, cancelled=cancelled
        )
        return _extraction(snapshot, response)

    def _exchange(
        self,
        arguments: tuple[str, ...],
        request: bytes,
        *,
        cancelled: Callable[[], bool],
    ) -> Mapping[str, object]:
        try:
            response = self._transport(
                self.executable,
                arguments,
                request,
                timeout_seconds=MAX_GRAPHIFY_SECONDS,
                max_output_bytes=MAX_GRAPHIFY_BYTES,
                cancelled=cancelled,
            )
        except GraphifyFailure:
            raise
        except TimeoutError:
            raise GraphifyFailure("timeout") from None
        except Exception:
            raise GraphifyFailure("worker_failed") from None
        if cancelled():
            raise GraphifyFailure("cancelled")
        value = _decode(response)
        if not isinstance(value, dict):
            raise GraphifyFailure("output_invalid")
        return MappingProxyType(value)


def discover_graphify_executable(base_executable: Path | None = None) -> Path:
    """Select the helper from the same installed prefix as the running base binary."""
    if base_executable is None:
        if not bool(getattr(sys, "frozen", False)):
            raise GraphifyFailure("adapter_unavailable")
        base_executable = Path(sys.executable)
    if not isinstance(base_executable, Path) or not base_executable.is_absolute():
        raise GraphifyFailure("adapter_unavailable")
    try:
        resolved_base = base_executable.resolve(strict=True)
        base_metadata = resolved_base.stat(follow_symlinks=False)
        if (
            resolved_base.name != "open-brain"
            or resolved_base.parent.name != "bin"
            or not stat.S_ISREG(base_metadata.st_mode)
        ):
            raise GraphifyFailure("adapter_unavailable")
        helper = resolved_base.parent.parent / "libexec/open-brain-graphify"
        helper_metadata = helper.stat(follow_symlinks=False)
        if (
            helper.is_symlink()
            or not stat.S_ISREG(helper_metadata.st_mode)
            or helper_metadata.st_mode & 0o111 == 0
        ):
            raise GraphifyFailure("adapter_unavailable")
        return helper
    except GraphifyFailure:
        raise
    except OSError:
        raise GraphifyFailure("adapter_unavailable") from None


def _component_identity() -> dict[str, object]:
    return {
        "graphify_version": GRAPHIFY_VERSION,
        "parser_profile": GRAPHIFY_PARSER_PROFILE,
        "patch_id": GRAPHIFY_PATCH_ID,
        "patch_sha256": GRAPHIFY_PATCH_SHA256,
        "upstream_commit": GRAPHIFY_UPSTREAM_COMMIT,
    }


def _capabilities() -> Mapping[str, object]:
    return {
        "component": _component_identity(),
        "max_input_bytes": MAX_GRAPHIFY_BYTES,
        "max_output_bytes": MAX_GRAPHIFY_BYTES,
        "operations": ["extract_markdown"],
        "protocol": GRAPHIFY_PROTOCOL,
    }


def _extraction(
    snapshot: ManagedGraphSnapshot, raw: Mapping[str, object]
) -> GraphifyExtraction:
    if set(raw) != {"diagnostics", "links", "pages", "protocol", "status"} or raw.get(
        "protocol"
    ) != GRAPHIFY_PROTOCOL:
        raise GraphifyFailure("output_invalid")
    status_value = raw.get("status")
    if status_value not in {"ok", "blocked"}:
        raise GraphifyFailure("output_invalid")
    source_ids = {source.note_id for source in snapshot.sources}
    pages = raw.get("pages")
    links_value = raw.get("links")
    diagnostics_value = raw.get("diagnostics")
    if (
        not isinstance(pages, list)
        or not all(isinstance(page, str) for page in pages)
        or not isinstance(links_value, list)
        or not isinstance(diagnostics_value, list)
        or len(links_value) > MAX_GRAPHIFY_ITEMS
        or len(diagnostics_value) > MAX_GRAPHIFY_ITEMS
    ):
        raise GraphifyFailure("output_invalid")
    if (
        status_value == "ok"
        and (set(cast(list[str], pages)) != source_ids or len(pages) != len(source_ids))
    ) or (status_value == "blocked" and pages):
        raise GraphifyFailure("output_invalid")
    links: list[GraphifyLink] = []
    for value in links_value:
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "source", "target"}
            or value.get("kind") != "explicit_reference"
            or value.get("source") not in source_ids
            or value.get("target") not in source_ids
            or value.get("source") == value.get("target")
        ):
            raise GraphifyFailure("output_invalid")
        links.append(
            GraphifyLink(cast(str, value["source"]), cast(str, value["target"]))
        )
    diagnostics: list[GraphifyDiagnostic] = []
    for value in diagnostics_value:
        if not isinstance(value, dict) or set(value) not in (
            {"code"},
            {"code", "source"},
        ):
            raise GraphifyFailure("output_invalid")
        code = value.get("code")
        source = value.get("source")
        if (
            code not in {"ambiguous_snapshot", "unresolved_reference"}
            or source is not None
            and source not in source_ids
        ):
            raise GraphifyFailure("output_invalid")
        diagnostics.append(GraphifyDiagnostic(cast(str, code), cast(str | None, source)))
    if status_value == "blocked" and (
        links or diagnostics != [GraphifyDiagnostic("ambiguous_snapshot")]
    ):
        raise GraphifyFailure("output_invalid")
    return GraphifyExtraction(
        snapshot.snapshot_sha256,
        status_value,
        tuple(sorted(links, key=lambda item: (item.source_note_id, item.target_note_id))),
        tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.source_note_id or "", item.code),
            )
        ),
    )


def _decode(raw: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, child in pairs:
            if key in value:
                raise GraphifyFailure("output_invalid")
            value[key] = child
        return value

    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_GRAPHIFY_BYTES:
            raise GraphifyFailure("output_invalid")
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _x: _reject())
        if portable_canonical_json_bytes(value) != raw:
            raise GraphifyFailure("output_invalid")
        return value
    except GraphifyFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise GraphifyFailure("output_invalid") from None


def _reject() -> None:
    raise GraphifyFailure("output_invalid")


__all__ = [
    "GRAPHIFY_PARSER_PROFILE",
    "GRAPHIFY_PATCH_ID",
    "GRAPHIFY_PATCH_SHA256",
    "GRAPHIFY_PROTOCOL",
    "GRAPHIFY_UPSTREAM_COMMIT",
    "GRAPHIFY_VERSION",
    "GraphifyAdapter",
    "GraphifyDiagnostic",
    "GraphifyExtraction",
    "GraphifyFailure",
    "GraphifyHelperTransport",
    "GraphifyLink",
    "SubprocessGraphifyTransport",
    "discover_graphify_executable",
]
