"""Consume locally synchronized YouTube transcripts through canonical capture ingress."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from open_brain_engine.capture.models import ShareRequest, ShareResponse
from open_brain_engine.engine import PublicJobCaptureSink

from open_brain_legacy._compat.open_brain.capture.http import enqueue_share
from open_brain_legacy.services.application import SingleUserLocalApplication

_SCHEMA_VERSION = 1
_MAX_SPOOL_BYTES = 110_000
_MAX_FILES = 50
_LAUNCHD_LABEL = re.compile(r"[A-Za-z0-9._-]+")


class ShareSubmitter(Protocol):
    def submit(self, request: ShareRequest) -> ShareResponse: ...


@dataclass(frozen=True, slots=True)
class PublicJobShareSubmitter:
    """Bounded YouTube spool adapter over one injected public-job capture sink."""

    sink: PublicJobCaptureSink

    def submit(self, request: ShareRequest) -> ShareResponse:
        if not isinstance(self.sink, PublicJobCaptureSink):
            raise ValueError("invalid public YouTube capture sink")
        return enqueue_share(request=request, capture=self.sink)


@dataclass(frozen=True, slots=True)
class SpoolConsumptionResult:
    processed: int
    failed: int


def consume_youtube_spool(
    root: Path,
    *,
    submitter: ShareSubmitter,
    max_files: int = _MAX_FILES,
) -> SpoolConsumptionResult:
    """Submit bounded spool records, deleting only durable queued duplicates or creates."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("invalid spool root")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= 500:
        raise ValueError("invalid spool limit")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("invalid spool root")

    processed = 0
    failed = 0
    for path in sorted(root.glob("*.json"))[:max_files]:
        try:
            request = _read_request(path)
            response = submitter.submit(request)
            if not isinstance(response, ShareResponse):
                raise ValueError("invalid share response")
            path.unlink()
            _fsync_directory(root)
            processed += 1
        except Exception:
            failed += 1
    return SpoolConsumptionResult(processed=processed, failed=failed)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="open-brain-youtube-bridge")
    parser.add_argument("--spool-root", type=Path, required=True)
    parser.add_argument("--kickstart-label")
    arguments = parser.parse_args(argv)
    env = os.environ if environment is None else environment
    try:
        root = env.get("OPEN_BRAIN_ROOT")
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            raise ValueError("invalid OPEN_BRAIN_ROOT")
        application = SingleUserLocalApplication.open(Path(root))
        submitter = PublicJobShareSubmitter(application.public_job_sink("JOB-029"))
        result = consume_youtube_spool(
            arguments.spool_root.expanduser().resolve(),
            submitter=submitter,
        )
        kickstart_failed = False
        if result.processed and arguments.kickstart_label:
            try:
                _kickstart(arguments.kickstart_label)
            except Exception:
                kickstart_failed = True
    except (OSError, ValueError):
        result = SpoolConsumptionResult(processed=0, failed=1)
        kickstart_failed = False

    print(
        json.dumps(
            {
                "failed": result.failed,
                "kickstart_failed": kickstart_failed,
                "processed": result.processed,
                "status": "ok"
                if result.failed == 0 and not kickstart_failed
                else "failed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if result.failed == 0 and not kickstart_failed else 1


def _read_request(path: Path) -> ShareRequest:
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid spool record")
    with path.open("rb") as stream:
        payload = stream.read(_MAX_SPOOL_BYTES + 1)
    if len(payload) > _MAX_SPOOL_BYTES:
        raise ValueError("spool record too large")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "privacy",
        "schema_version",
        "text",
        "url",
        "why",
    }:
        raise ValueError("invalid spool record")
    if value["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("invalid spool schema")
    return ShareRequest.create(
        url=_string(value["url"]),
        why=_string(value["why"]),
        text=_string(value["text"]),
        privacy_tier=_string(value["privacy"]),
    )


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid spool field")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _kickstart(label: str) -> None:
    if not _LAUNCHD_LABEL.fullmatch(label):
        raise ValueError("invalid launchd label")
    subprocess.run(
        ("/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{label}"),
        check=True,
        capture_output=True,
        timeout=10,
    )


if __name__ == "__main__":
    raise SystemExit(main())
