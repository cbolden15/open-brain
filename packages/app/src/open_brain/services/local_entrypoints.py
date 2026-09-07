"""Direct local CLI for the default Open Brain product."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import NoReturn, cast

from open_brain_engine import __version__
from open_brain_engine.engine import (
    CaptureReceipt,
    PortabilityReceipt,
    RetrievalResult,
    TextPayload,
    canonical_json_bytes,
    read_maintenance_snapshot,
)
from open_brain_engine.storage.operational import (
    StorageError,
    atomic_replace,
    capture_root_identity,
    read_confined,
    read_confined_tree,
)

from open_brain.local_data import FilesystemTypeProbe, LocalDataError, select_local_root
from open_brain.profile import ProfileError
from open_brain.services.local_bootstrap import (
    LocalBrainSession,
    LocalRuntimeConflictError,
    initialize_local_brain,
    open_local_brain,
)

_DOCTOR_CHECKS = (
    "private-data-directory",
    "no-background-runtime",
    "base-dependency-closure",
)
_BASE_DEPENDENCY_REQUIREMENTS = {
    "open-brain": ("open-brain-engine==0.1.0",),
    "open-brain-engine": ("rfc8785<0.2,>=0.1.4",),
    "rfc8785": (),
}
_LOCAL_EXPORT_EVIDENCE = ".open-brain/state/local-export-evidence.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EXPORT_ID = re.compile(
    r"^export_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class _UsageError(ValueError):
    pass


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise _UsageError("invalid command")


def run_cli(
    argv: tuple[str, ...] | list[str] | None = None,
    *,
    environment: Mapping[str, object] | None = None,
    platform_name: str | None = None,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> int:
    """Run one daemonless local command without loading Secure Node composition."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    try:
        parsed = _parser().parse_args(arguments)
    except _UsageError:
        _write_usage_failure(json_output=json_output)
        return 2
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    if parsed.command is None:
        _write_usage_failure(json_output=json_output)
        return 2
    selected_environment = os.environ if environment is None else environment
    json_output = bool(getattr(parsed, "json", False))
    try:
        selection = select_local_root(
            data_dir=getattr(parsed, "data_dir", None),
            environment=selected_environment,
            platform_name=platform_name,
        )
    except LocalDataError:
        _write_private_data_failure(json_output=json_output)
        return 78
    try:
        if parsed.command == "init":
            receipt = initialize_local_brain(
                selection,
                filesystem_type_probe=filesystem_type_probe,
            )
            if json_output:
                _write_json(receipt.to_dict())
            else:
                print(
                    f"Open Brain {receipt.status}. Profile: local. Storage: SQLite. "
                    "Application encryption: false."
                )
            return 0
        with open_local_brain(
            selection,
            filesystem_type_probe=filesystem_type_probe,
        ) as session:
            return _run_local_command(parsed, session, json_output=json_output)
    except (LocalDataError, ProfileError):
        _write_private_data_failure(json_output=json_output)
        return 78
    except Exception:
        _write_operation_failure(json_output=json_output)
        return 78


def _parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(
        prog="open-brain",
        description="Private, daemonless local Brain.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_local_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    init_parser = subparsers.add_parser(
        "init", help="Create or reopen one private local Brain."
    )
    _add_local_options(init_parser)
    capture_parser = subparsers.add_parser(
        "capture", help="Capture owner-authored text directly into the local Brain."
    )
    _add_local_options(capture_parser)
    capture_parser.add_argument("text", help="Text to capture.")
    search_parser = subparsers.add_parser(
        "search", help="Search the local Brain directly."
    )
    _add_local_options(search_parser)
    search_parser.add_argument("query", help="Text to find.")
    export_parser = subparsers.add_parser(
        "export", help="Create a full Portable Brain export."
    )
    _add_local_options(export_parser)
    export_parser.add_argument("destination", help="New export directory.")
    export_parser.add_argument(
        "--verify",
        action="store_true",
        help="Validate the promoted export before reporting success.",
    )
    status_parser = subparsers.add_parser(
        "status", help="Report bounded default-product status."
    )
    _add_local_options(status_parser)
    doctor_parser = subparsers.add_parser(
        "doctor", help="Run one bounded default-product check."
    )
    _add_local_options(doctor_parser)
    doctor_parser.add_argument("--check", required=True, choices=_DOCTOR_CHECKS)
    return parser


def _add_local_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write JSON output.",
    )
    parser.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        help="Use this absolute Brain root instead of the platform-local default.",
    )


def _run_local_command(
    parsed: argparse.Namespace,
    session: LocalBrainSession,
    *,
    json_output: bool,
) -> int:
    if parsed.command == "status":
        _write_status(session, json_output=json_output)
        return 0
    if parsed.command == "doctor":
        return _write_doctor(
            session,
            cast(str, parsed.check),
            json_output=json_output,
        )
    tasks = session.tasks
    if tasks is None:
        raise LocalRuntimeConflictError("background runtime is active")
    if parsed.command == "capture":
        capture_receipt = tasks.capture.accept(
            TextPayload(cast(str, parsed.text)),
            delivery_id="delivery." + str(uuid.uuid4()),
        )
        _write_capture(capture_receipt, json_output=json_output)
        return 0
    if parsed.command == "search":
        results = tasks.retrieval.search(cast(str, parsed.query))
        _write_search(results, json_output=json_output)
        return 0
    if parsed.command == "export":
        destination = _absolute_destination(cast(str, parsed.destination))
        export_id = "export_" + str(uuid.uuid4())
        export_receipt = tasks.portability.export(destination, export_id=export_id)
        verification = "not_requested"
        if bool(parsed.verify):
            tasks.portability.validate(destination)
            _record_verified_export(session, destination, export_id=export_id)
            verification = "verified"
        _write_export(export_receipt, verification=verification, json_output=json_output)
        return 0
    raise ValueError("invalid local command")


def _write_capture(receipt: CaptureReceipt, *, json_output: bool) -> None:
    payload = {
        "capture_id": receipt.capture_id,
        "duplicate": receipt.duplicate,
        "payload_family": receipt.payload_family,
        "state": receipt.state,
        "status": "captured",
    }
    if json_output:
        _write_json(payload)
    else:
        print(f"Captured {receipt.capture_id}")


def _write_search(results: tuple[RetrievalResult, ...], *, json_output: bool) -> None:
    payload = {
        "results": [
            {
                "capture_id": result.capture_id,
                "excerpt": result.excerpt,
                "payload_family": result.payload_family,
                "record_type": result.record_type,
                "result_id": result.result_id,
                "title": result.title,
                "trust": result.trust,
            }
            for result in results
        ],
        "status": "ok",
    }
    if json_output:
        _write_json(payload)
    else:
        for result in results:
            print(result.excerpt)


def _write_export(
    receipt: PortabilityReceipt,
    *,
    verification: str,
    json_output: bool,
) -> None:
    payload = {
        "captures": receipt.captures,
        "history_records": receipt.history_records,
        "portable_files": receipt.portable_files,
        "status": receipt.status,
        "verification": verification,
    }
    if json_output:
        _write_json(payload)
    else:
        print(
            "Portable Brain export verified."
            if verification == "verified"
            else "Portable Brain export created."
        )


def _write_status(session: LocalBrainSession, *, json_output: bool) -> None:
    maintenance = read_maintenance_snapshot(session.profile)
    daemon_running = "daemon-authority" in maintenance.writer.held_leases
    payload = {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": daemon_running,
        "portable_export": _verified_export_state(session),
        "profile": "local",
        "storage": "sqlite",
    }
    if json_output:
        _write_json(payload)
    else:
        print(
            "Profile: local. Brain count: 1. Storage: SQLite. "
            f"Portable export: {payload['portable_export']}. "
            f"Daemon running: {str(daemon_running).lower()}. "
            "Application encryption: false."
        )


def _write_doctor(
    session: LocalBrainSession,
    check: str,
    *,
    json_output: bool,
) -> int:
    checks = {
        "private-data-directory": _private_data_directory_is_safe,
        "no-background-runtime": _no_background_runtime,
        "base-dependency-closure": _base_dependency_closure_is_safe,
    }
    try:
        passed = checks[check](session)
    except (KeyError, OSError, StorageError, ValueError):
        passed = False
    payload = {"check": check, "status": "ok" if passed else "failed"}
    if json_output:
        _write_json(payload)
    else:
        print(f"{check}: {payload['status']}")
    return 0 if passed else 1


def _private_data_directory_is_safe(session: LocalBrainSession) -> bool:
    session.prepared.revalidate()
    return session.prepared.private_file_exists(
        "brain.toml"
    ) and session.prepared.private_file_exists(".open-brain/state/phase1.sqlite3")


def _no_background_runtime(session: LocalBrainSession) -> bool:
    session.prepared.revalidate()
    maintenance = read_maintenance_snapshot(session.profile)
    if maintenance.writer.held_count or maintenance.writer.malformed_count:
        return False
    runtime_files = read_confined_tree(
        root=session.profile.root,
        relative=".open-brain/run",
        expected_root_identity=session.profile.root_identity,
        maximum_entries=32,
        maximum_file_bytes=64 * 1024,
        maximum_total_bytes=128 * 1024,
    )
    session.prepared.revalidate()
    return not runtime_files


def _base_dependency_closure_is_safe(_session: LocalBrainSession) -> bool:
    for distribution, expected in _BASE_DEPENDENCY_REQUIREMENTS.items():
        try:
            requirements = importlib.metadata.requires(distribution)
        except importlib.metadata.PackageNotFoundError:
            return False
        unconditional = tuple(
            sorted(
                requirement
                for requirement in requirements or ()
                if "extra ==" not in requirement.casefold()
            )
        )
        if unconditional != expected:
            return False
    return True


def _absolute_destination(value: str) -> Path:
    if not value or "\x00" in value:
        raise ValueError("invalid export destination")
    destination = Path(value)
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("invalid export destination")
    return destination


def _record_verified_export(
    session: LocalBrainSession,
    destination: Path,
    *,
    export_id: str,
) -> None:
    destination_identity = capture_root_identity(destination)
    manifest = read_confined(
        root=destination,
        relative="portable-manifest.json",
        expected_root_identity=destination_identity,
        maximum_bytes=1024 * 1024,
    )
    if manifest is None:
        raise ValueError("Portable export manifest is unavailable")
    session.prepared.revalidate()
    atomic_replace(
        root=session.profile.root,
        relative=_LOCAL_EXPORT_EVIDENCE,
        data=canonical_json_bytes(
            {
                "created_at": _timestamp(datetime.now(UTC)),
                "export_id": export_id,
                "manifest_digest_sha256": sha256(manifest).hexdigest(),
                "schema_version": 1,
            }
        ),
        expected_root_identity=session.profile.root_identity,
    )
    session.prepared.revalidate()


def _verified_export_state(session: LocalBrainSession) -> str:
    payload = read_confined(
        root=session.profile.root,
        relative=_LOCAL_EXPORT_EVIDENCE,
        expected_root_identity=session.profile.root_identity,
        maximum_bytes=4096,
    )
    if payload is None:
        return "absent"
    try:
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "created_at",
                "export_id",
                "manifest_digest_sha256",
                "schema_version",
            }
            or value["schema_version"] != 1
            or canonical_json_bytes(value) != payload
            or not isinstance(value["created_at"], str)
            or not isinstance(value["export_id"], str)
            or not isinstance(value["manifest_digest_sha256"], str)
            or _EXPORT_ID.fullmatch(value["export_id"]) is None
            or _HEX64.fullmatch(value["manifest_digest_sha256"]) is None
        ):
            return "invalid"
        datetime.fromisoformat(value["created_at"].removesuffix("Z") + "+00:00")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "invalid"
    return "verified"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")))


def _write_private_data_failure(*, json_output: bool) -> None:
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "private_data_directory_unavailable",
                    "message": "Open Brain could not use the private data directory.",
                },
                "status": "failed",
            }
        )
    else:
        print("Open Brain could not use the private data directory.", file=sys.stderr)


def _write_usage_failure(*, json_output: bool) -> None:
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "invalid_command",
                    "message": "Open Brain could not parse the command.",
                },
                "status": "failed",
            }
        )
    else:
        print("Open Brain could not parse the command.", file=sys.stderr)


def _write_operation_failure(*, json_output: bool) -> None:
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "local_operation_failed",
                    "message": "Open Brain could not complete the local command.",
                },
                "status": "failed",
            }
        )
    else:
        print("Open Brain could not complete the local command.", file=sys.stderr)


__all__ = ["run_cli"]
