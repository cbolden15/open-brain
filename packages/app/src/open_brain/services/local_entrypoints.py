"""Direct local CLI for the default Open Brain product."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import select
import signal
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import NoReturn, cast

from open_brain_engine import __version__
from open_brain_engine.engine import (
    CaptureReceipt,
    EngineTaskSet,
    MarkdownImportCancelled,
    MarkdownImportEntry,
    MarkdownImportFailure,
    MarkdownImportInterrupted,
    MarkdownImportPreflight,
    MarkdownImportProgress,
    MarkdownImportSummary,
    PortabilityReceipt,
    RetrievalResult,
    TextPayload,
    canonical_json_bytes,
    live_search_is_healthy,
    read_maintenance_snapshot,
)
from open_brain_engine.storage.locks import LockBusyError
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
    "search-index",
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
    if parsed.command != "import":
        return _run_parsed_command(
            parsed,
            environment=selected_environment,
            json_output=json_output,
            platform_name=platform_name,
            filesystem_type_probe=filesystem_type_probe,
            import_interrupted=None,
        )

    interrupted = False

    def request_interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    def should_interrupt() -> bool:
        return interrupted

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, request_interrupt)
    try:
        return _run_parsed_command(
            parsed,
            environment=selected_environment,
            json_output=json_output,
            platform_name=platform_name,
            filesystem_type_probe=filesystem_type_probe,
            import_interrupted=should_interrupt,
        )
    except KeyboardInterrupt:
        _write_import_interrupted(json_output=json_output)
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _run_parsed_command(
    parsed: argparse.Namespace,
    *,
    environment: Mapping[str, object],
    json_output: bool,
    platform_name: str | None,
    filesystem_type_probe: FilesystemTypeProbe | None,
    import_interrupted: Callable[[], bool] | None,
) -> int:
    try:
        selection = select_local_root(
            data_dir=getattr(parsed, "data_dir", None),
            environment=environment,
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
            return _run_local_command(
                parsed,
                session,
                json_output=json_output,
                import_interrupted=import_interrupted,
            )
    except LocalDataError, ProfileError:
        _write_private_data_failure(json_output=json_output)
        return 78
    except LockBusyError:
        if parsed.command == "import":
            _write_import_busy(json_output=json_output)
            return 75
        _write_operation_failure(json_output=json_output)
        return 78
    except MarkdownImportCancelled:
        print("Markdown import cancelled; no import state was written.", file=sys.stderr)
        return 0
    except MarkdownImportInterrupted:
        _write_import_interrupted(json_output=json_output)
        return 130
    except MarkdownImportFailure as error:
        _write_import_failure(error, json_output=json_output)
        return 78
    except Exception:
        if parsed.command == "import":
            _write_import_operation_failure(json_output=json_output)
            return 78
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
    init_parser = subparsers.add_parser("init", help="Create or reopen one private local Brain.")
    _add_local_options(init_parser)
    capture_parser = subparsers.add_parser(
        "capture", help="Capture owner-authored text directly into the local Brain."
    )
    _add_local_options(capture_parser)
    capture_parser.add_argument("text", help="Text to capture.")
    import_parser = subparsers.add_parser(
        "import",
        help="Import a Markdown directory into immutable local history.",
        description=(
            "Import one Markdown directory. Imported revisions remain in history and export "
            "after source removal."
        ),
    )
    _add_local_options(import_parser)
    import_parser.add_argument("directory", help="Existing absolute Markdown directory.")
    import_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm a new import root without prompting.",
    )
    import_parser.add_argument(
        "--allow-large-vault",
        action="store_true",
        help="Allow aggregate scan bounds; the one-file limit still applies.",
    )
    search_parser = subparsers.add_parser("search", help="Search the local Brain directly.")
    _add_local_options(search_parser)
    search_parser.add_argument("query", help="Text to find.")
    search_parser.add_argument("--limit", type=int, default=10, help="Return 1 to 100 results.")
    export_parser = subparsers.add_parser("export", help="Create a full Portable Brain export.")
    _add_local_options(export_parser)
    export_parser.add_argument("destination", help="New export directory.")
    export_parser.add_argument(
        "--verify",
        action="store_true",
        help="Validate the promoted export before reporting success.",
    )
    status_parser = subparsers.add_parser("status", help="Report bounded default-product status.")
    _add_local_options(status_parser)
    doctor_parser = subparsers.add_parser("doctor", help="Run one bounded default-product check.")
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
    import_interrupted: Callable[[], bool] | None,
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
    if parsed.command == "import":
        if import_interrupted is None:
            raise ValueError("import interruption callback is unavailable")
        return _run_markdown_import(
            parsed,
            tasks,
            json_output=json_output,
            interrupted=import_interrupted,
        )
    if parsed.command == "search":
        tasks.reconciliation.reconcile()
        results = tasks.retrieval.search(
            cast(str, parsed.query),
            limit=cast(int, parsed.limit),
        )
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


def _run_markdown_import(
    parsed: argparse.Namespace,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
    interrupted: Callable[[], bool],
) -> int:
    def accept_import(_preflight: MarkdownImportPreflight) -> bool:
        return True

    def prompt_for_import(preflight: MarkdownImportPreflight) -> bool:
        return _confirm_markdown_import(preflight, interrupted)

    confirm: Callable[[MarkdownImportPreflight], bool] | None
    if bool(parsed.yes):
        confirm = accept_import
    elif json_output or not (sys.stdin.isatty() and sys.stderr.isatty()):
        confirm = None
    else:
        confirm = prompt_for_import
    progress = None if json_output or not sys.stderr.isatty() else _write_import_progress
    summary = tasks.markdown_import.import_directory(
        cast(str, parsed.directory),
        allow_large_vault=bool(parsed.allow_large_vault),
        confirm=confirm,
        progress=progress,
        interrupted=interrupted,
    )
    _write_import_summary(summary, json_output=json_output)
    return 1 if summary.failed else 0


def _confirm_markdown_import(
    preflight: MarkdownImportPreflight,
    interrupted: Callable[[], bool],
) -> bool:
    print(
        f"Markdown directory: {_terminal_text(os.fspath(preflight.canonical_path))}\n"
        f"Selected Markdown files: {preflight.selected_markdown_files}\n"
        f"Aggregate bytes: {preflight.aggregate_bytes}\n"
        "Imported revisions remain in history and export after source removal.",
        file=sys.stderr,
    )
    print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
    deadline = time.monotonic() + 60.0
    while True:
        if interrupted():
            raise MarkdownImportInterrupted
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(file=sys.stderr)
            return False
        try:
            descriptor = sys.stdin.fileno()
        except AttributeError, OSError:
            print(file=sys.stderr)
            return False
        readable, _, _ = select.select((descriptor,), (), (), min(remaining, 0.25))
        if readable:
            answer = sys.stdin.readline()
            break
    return answer.strip().lower() in {"y", "yes"} and answer.strip().isascii()


def _write_import_summary(summary: MarkdownImportSummary, *, json_output: bool) -> None:
    if json_output:
        _write_json(summary.to_dict())
        return
    print(
        "Markdown import: "
        f"selected={summary.selected} imported={summary.imported} updated={summary.updated} "
        f"unchanged={summary.unchanged} missing={summary.missing} skipped={summary.skipped} "
        f"failed={summary.failed} missing_finalized={str(summary.missing_finalized).lower()}"
    )
    if summary.selected == 0:
        print("No eligible Markdown files found.")
    for entry in summary.entries:
        print(_terminal_text(_human_import_entry(entry)))
    if summary.entries_omitted:
        noun = "result" if summary.entries_omitted == 1 else "results"
        print(
            f"{summary.entries_omitted} additional {noun} omitted; "
            "counts above include the full run."
        )


def _human_import_entry(entry: MarkdownImportEntry) -> str:
    suffix = "" if entry.reason is None else f" ({entry.reason})"
    return f"{entry.outcome} {entry.path}{suffix}"


def _write_import_progress(progress: MarkdownImportProgress) -> None:
    if progress.processed_markdown_files:
        print(
            f"Markdown import progress: processed={progress.processed_markdown_files}",
            file=sys.stderr,
        )
    else:
        print(
            f"Markdown import progress: visited={progress.visited_entries}",
            file=sys.stderr,
        )


def _write_import_busy(*, json_output: bool) -> None:
    message = "Another Open Brain command is using this Brain. Retry after it finishes."
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "local_operation_busy",
                    "details": {"missing_finalized": False},
                    "message": message,
                },
                "status": "failed",
            }
        )
    else:
        print(message, file=sys.stderr)


def _write_import_operation_failure(*, json_output: bool) -> None:
    message = "Open Brain could not complete the local command."
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "local_operation_failed",
                    "details": {"missing_finalized": False},
                    "message": message,
                },
                "status": "failed",
            }
        )
    else:
        print(message, file=sys.stderr)


def _write_import_interrupted(*, json_output: bool) -> None:
    json_message = (
        "Markdown import interrupted. Completed file commits were kept; "
        "rerun the same command to resume."
    )
    if json_output:
        _write_json(
            {
                "error": {
                    "code": "import_interrupted",
                    "details": {"missing_finalized": False},
                    "message": json_message,
                },
                "status": "interrupted",
            }
        )
    else:
        print(
            "Markdown import interrupted. Completed file commits were kept; missing paths were "
            "not finalized. Rerun the same command to resume.",
            file=sys.stderr,
        )


def _write_import_failure(error: MarkdownImportFailure, *, json_output: bool) -> None:
    details = dict(error.details)
    message = _import_failure_message(error.code, details)
    if json_output:
        _write_json(
            {
                "error": {"code": error.code, "details": details, "message": message},
                "status": "failed",
            }
        )
    else:
        print(message, file=sys.stderr)


def _import_failure_message(code: str, details: Mapping[str, object]) -> str:
    if code == "import_confirmation_required":
        return (
            "Import requires confirmation. Review the selected directory and retention warning, "
            "then retry with --yes."
        )
    if code == "import_directory_unavailable":
        return "Markdown import requires an existing absolute directory."
    if code == "large_vault_confirmation_required":
        return "Vault exceeds the default import limits. Retry with --allow-large-vault."
    if code == "import_scan_incomplete":
        return (
            "Markdown import could not complete the directory scan. Fix directory access or "
            "filesystem changes, then retry."
        )
    if code == "import_root_changed":
        return (
            "The selected directory no longer matches registered import "
            f"{details.get('root_id', 'unknown')}. Restore it at its registered location, or copy "
            "the content to a new disjoint directory."
        )
    if code == "overlapping_import_root" and details.get("conflict") == "brain":
        return (
            "The selected directory overlaps Open Brain data. Choose a directory outside the "
            "Brain data tree."
        )
    if code == "overlapping_import_root":
        return (
            "The selected directory overlaps registered import "
            f"{details.get('root_id', 'unknown')}. Use that registered import or choose a "
            "disjoint directory."
        )
    raise ValueError("invalid Markdown import failure")


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
                "source_origin": result.provenance.source_origin,
                "title": result.title,
                "trust": result.trust,
                "explanation": result.explanation,
            }
            for result in results
        ],
        "status": "ok",
    }
    if json_output:
        _write_json(payload)
    else:
        for result in results:
            print(
                _terminal_text(
                    f"[{_human_trust(result)} | {result.provenance.source_origin}] "
                    f"{result.title}: {result.excerpt}"
                )
            )


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
        "live_search": maintenance.live_search.to_dict(),
        "portable_export": _verified_export_state(session),
        "portable_snapshot": maintenance.index.to_dict(),
        "profile": "local",
        "storage": "sqlite",
    }
    if json_output:
        _write_json(payload)
    else:
        print(
            "Profile: local. Brain count: 1. Storage: SQLite. "
            f"Live search: {maintenance.live_search.state} "
            f"({maintenance.live_search.fts_count} documents). "
            f"Portable snapshot: {maintenance.index.state}, non-authoritative, "
            "potentially stale. "
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
        "search-index": _search_index_is_healthy,
    }
    try:
        passed = checks[check](session)
    except KeyError, OSError, StorageError, ValueError:
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


def _search_index_is_healthy(session: LocalBrainSession) -> bool:
    session.prepared.revalidate()
    healthy = live_search_is_healthy(session.profile)
    session.prepared.revalidate()
    return healthy


def _human_trust(result: RetrievalResult) -> str:
    if result.provenance.source_origin == "owner_authored":
        return "owner"
    if result.provenance.source_origin == "third_party":
        return "third-party"
    return "unverified"


def _terminal_text(value: str) -> str:
    safe = "".join(
        " "
        if ord(character) < 32
        or 0x7F <= ord(character) <= 0x9F
        or unicodedata.category(character) == "Cf"
        else character
        for character in value
    )
    return " ".join(safe.split())


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
    except KeyError, TypeError, ValueError, json.JSONDecodeError:
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
