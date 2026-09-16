"""Explicit file snapshots and preview-bound document extraction."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.document_parser import (
    MAX_FILE_BYTES,
    MAX_TEXT,
    MEMORY_BYTES,
    WALL_SECONDS,
)
from open_brain_connectors.runtime.local_document import LocalDocumentRecord


def read_selected_file(path: Path, *, maximum: int = MAX_FILE_BYTES) -> tuple[Path, bytes]:
    """Open a regular leaf without following links; return an immutable snapshot."""
    parent_fd = -1
    try:
        parent = path.expanduser().absolute().parent.resolve(strict=True)
        selected = parent / path.name
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = os.fstat(parent_fd)
        fd = os.open(
            selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd,
        )
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ConnectorContractError("document_not_regular")
            if before.st_size > maximum:
                raise ConnectorContractError("document_file_too_large")
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        current = os.stat(selected.name, dir_fd=parent_fd, follow_symlinks=False)
        current_parent = parent.stat(follow_symlinks=False)
        if (directory.st_dev, directory.st_ino) != (current_parent.st_dev, current_parent.st_ino):
            raise ConnectorContractError("document_file_changed")
        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ConnectorContractError("document_file_changed")
        if len(data) > maximum:
            raise ConnectorContractError("document_file_too_large")
        return selected, data
    except ConnectorContractError:
        raise
    except (OSError, ValueError) as error:
        raise ConnectorContractError("document_file_unavailable") from error
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _extract(data: bytes, kind: str) -> str:
    process = subprocess.Popen(
        [sys.executable, "-I", "-m",
         "open_brain_connectors.runtime.document_parser", kind],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
    )
    stopped = threading.Event()
    memory_exceeded = threading.Event()

    def watch_memory() -> None:
        # Darwin's RLIMIT_AS is advisory. Bound sustained RSS as well as wall/CPU
        # time; ps reports KiB. A transient allocation can exceed this threshold.
        while not stopped.wait(0.05):
            try:
                result = subprocess.run(
                    ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                    capture_output=True, timeout=1, check=False,
                )
                if int(result.stdout.strip() or b"0") * 1024 > MEMORY_BYTES:
                    memory_exceeded.set()
                    process.kill()
                    return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                process.kill()
                return

    watcher = threading.Thread(target=watch_memory, daemon=True)
    if sys.platform == "darwin":
        watcher.start()
    try:
        try:
            output, _ = process.communicate(data, timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise ConnectorContractError("document_parser_timeout") from error
        if memory_exceeded.is_set():
            raise ConnectorContractError("document_parser_memory_limit")
        if process.returncode != 0 or len(output) > MAX_TEXT * 12 + 1024:
            raise ConnectorContractError("document_parser_failed")
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        if watcher.is_alive():
            watcher.join(timeout=2)
    try:
        result = json.loads(output)
        if type(result) is not dict:
            raise ValueError
        if "error" in result:
            allowed = {
                "document_unsupported_xml", "document_encrypted_or_legacy",
                "document_archive_limit", "document_encrypted", "document_invalid",
                "document_text_too_large", "document_no_text", "document_file_too_large",
                "document_page_limit", "document_stream_limit",
            }
            code = result["error"]
            raise ConnectorContractError(code if code in allowed else "document_parser_failed")
        text = result["text"]
        if type(text) is not str or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError
        return text
    except (ValueError, KeyError, TypeError) as error:
        if isinstance(error, ConnectorContractError):
            raise
        raise ConnectorContractError("document_parser_failed") from error


@dataclass(frozen=True)
class SelectedDocument:
    record: LocalDocumentRecord
    preview_id: str
    connection_id: str


def extract_selected_document(
    path: Path, *, title: str, connection_id: str,
) -> SelectedDocument:
    kind = {".pdf": "text_pdf", ".docx": "docx_file"}.get(path.suffix.lower())
    if kind is None:
        raise ConnectorContractError("document_unsupported_format")
    selected, data = read_selected_file(path)
    document_id = "document:" + hashlib.sha256(os.fsencode(selected)).hexdigest()
    revision_id = hashlib.sha256(data).hexdigest()
    text = _extract(data, kind)
    record = LocalDocumentRecord(
        document_id=document_id, revision_id=revision_id, file_kind=kind,
        title=title, text=text,
    )
    # Binding includes the extracted result and title, not just mutable file bytes.
    binding = json.dumps(
        ["document-preview-v1", connection_id, document_id, revision_id, kind,
         record.title, record.text], ensure_ascii=True, separators=(",", ":"),
    ).encode()
    return SelectedDocument(record, hashlib.sha256(binding).hexdigest(), connection_id)
