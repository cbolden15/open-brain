"""Direct local CLI for the default Open Brain product."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import select
import signal
import stat
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
    ManagedAccessMode,
    ManagedProvider,
    ManagedWorkspaceReceipt,
    MarkdownImportCancelled,
    MarkdownImportEntry,
    MarkdownImportFailure,
    MarkdownImportInterrupted,
    MarkdownImportPreflight,
    MarkdownImportProgress,
    MarkdownImportSummary,
    PortabilityReceipt,
    RetrievalResult,
    canonical_json_bytes,
    live_search_is_healthy,
    read_maintenance_snapshot,
)
from open_brain_engine.engine.contracts import ManagedWorkspaceFailure
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.operational import (
    StorageError,
    atomic_replace,
    capture_root_identity,
    read_confined,
)

from open_brain.local_data import FilesystemTypeProbe, LocalDataError, select_local_root
from open_brain.profile import ProfileError
from open_brain.services.agent_setup import (
    AgentSetupFailure,
    apply_agent_setup,
    preview_agent_setup,
    resolve_agent_runtime,
)
from open_brain.services.local_bootstrap import (
    LocalBrainSession,
    initialize_local_brain,
    open_local_brain,
)
from open_brain.services.local_operations import (
    capture_result,
    capture_text,
    database_is_busy,
    graph_canvas,
    graph_projection,
    graph_suggestions,
    mcp_capture_sink,
    refresh_structural_graph,
    search_brain,
    search_result,
)
from open_brain.services.local_operations import (
    workspace_status as workspace_status_result,
)
from open_brain.services.local_runtime_session import LocalRuntimeCompatibilityError
from open_brain.services.managed_recovery import (
    ManagedRecoveryCommandFailure,
    run_managed_recovery,
)
from open_brain.services.review_publication import (
    MAX_REVIEW_MARKDOWN_BYTES,
    ReviewPublicationError,
    ReviewPublicationService,
    validate_review_arguments,
)
from open_brain.services.space_inbox import (
    SpaceInboxError,
    SpaceInboxService,
    validate_space_inbox_arguments,
)
from open_brain.services.t03_adapters import (
    T03AppAdapter,
    T03AppError,
    agent_authority,
    error_result,
    owner_authority,
)

_DOCTOR_CHECKS = (
    "private-data-directory",
    "foreground-runtime",
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
    if parsed.command == "workspace":
        try:
            _validate_workspace_recovery_arguments(parsed)
        except ValueError:
            _write_usage_failure(json_output=json_output)
            return 2
    if parsed.command == "mcp" and (
        not (
            parsed.allow_capture
            or parsed.allow_search
            or parsed.allow_content_read
            or parsed.allow_history_read
            or parsed.allow_workspace_read
            or parsed.allow_graph_refresh
            or parsed.allow_inbox_read
            or parsed.allow_organize
            or parsed.allow_review_read
            or parsed.allow_review_propose
            or parsed.allow_review_decide
        )
        or json_output
    ):
        _write_usage_failure(json_output=False)
        return 2
    selected_environment = os.environ if environment is None else environment
    json_output = bool(getattr(parsed, "json", False))
    if parsed.command in {"space", "inbox"}:
        try:
            operation, organization_arguments = _space_inbox_arguments(parsed)
            validate_space_inbox_arguments(operation, organization_arguments)
        except SpaceInboxError:
            _write_usage_failure(json_output=json_output)
            return 2
    if parsed.command == "review":
        try:
            review_operation, review_arguments = _review_arguments(parsed)
            validate_review_arguments(review_operation, review_arguments)
            parsed.review_arguments = review_arguments
        except ReviewPublicationError:
            _write_usage_failure(json_output=json_output)
            return 2
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
        if _is_workspace_recovery(parsed):
            return _write_managed_recovery_failure(
                "private_data_unavailable",
                schema_upgraded=False,
                json_output=json_output,
            )
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
        if parsed.command == "plugin":
            from open_brain.services.plugin_bridge import serve_plugin_stdio

            return serve_plugin_stdio(
                selection,
                input_stream=sys.stdin.buffer,
                output_stream=sys.stdout.buffer,
                filesystem_type_probe=filesystem_type_probe,
                environment=environment,
            )
        if parsed.command == "agent":
            return _run_agent_setup(
                parsed,
                selection=selection,
                environment=environment,
                json_output=json_output,
            )
        if _is_workspace_recovery(parsed):
            payload = run_managed_recovery(
                selection,
                operation_id=getattr(parsed, "operation_id", None),
                after=getattr(parsed, "after", None),
                limit=getattr(parsed, "limit", None) or 100,
                abandon=bool(getattr(parsed, "abandon", False)),
                expected_digest=getattr(parsed, "expected_digest", None),
                request_id=getattr(parsed, "request_id", None),
                filesystem_type_probe=filesystem_type_probe,
            )
            _write_managed(payload, json_output=json_output)
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
    except ManagedWorkspaceFailure as error:
        if error.code == "workspace_recovery_required":
            message = "A legacy workspace request needs owner recovery. Use workspace recover."
            if json_output:
                _write_json({"error": {"code": error.code, "message": message}})
            else:
                print(f"{error.code}: {message}", file=sys.stderr)
            return 78
        _write_operation_failure(json_output=json_output)
        return 78
    except ManagedRecoveryCommandFailure as error:
        return _write_managed_recovery_failure(
            error.code,
            schema_upgraded=error.schema_upgraded,
            json_output=json_output,
        )
    except LocalRuntimeCompatibilityError:
        if _is_workspace_recovery(parsed):
            return _write_managed_recovery_failure(
                "runtime_in_use",
                schema_upgraded=False,
                json_output=json_output,
            )
        _write_operation_failure(json_output=json_output)
        return 78
    except LocalDataError, ProfileError:
        if _is_workspace_recovery(parsed):
            return _write_managed_recovery_failure(
                "private_data_unavailable",
                schema_upgraded=False,
                json_output=json_output,
            )
        _write_private_data_failure(json_output=json_output)
        return 78
    except AgentSetupFailure as error:
        return _write_agent_setup_failure(error.code, json_output=json_output)
    except SpaceInboxError as error:
        return _write_space_inbox_failure(error.code, json_output=json_output)
    except ReviewPublicationError as error:
        return _write_review_failure(error.code, json_output=json_output)
    except T03AppError as error:
        if json_output:
            _write_json(error_result(error.code))
        else:
            print(error.code, file=sys.stderr)
        return 2 if error.code == "invalid_arguments" else 1
    except LockBusyError:
        if _is_workspace_recovery(parsed):
            return _write_managed_recovery_failure(
                "database_busy",
                schema_upgraded=False,
                json_output=json_output,
            )
        if parsed.command in {"capture", "search", "mcp", "space", "inbox", "review"}:
            _write_database_busy(json_output=json_output)
            return 75
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
    except Exception as error:
        if _is_workspace_recovery(parsed):
            return _write_managed_recovery_failure(
                "managed_recovery_failed",
                schema_upgraded=False,
                json_output=json_output,
            )
        if parsed.command in {
            "capture",
            "search",
            "mcp",
            "space",
            "inbox",
            "review",
        } and database_is_busy(error):
            _write_database_busy(json_output=json_output)
            return 75
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
    _add_t03_parsers(subparsers)
    _add_space_inbox_parsers(subparsers)
    _add_review_parsers(subparsers)
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Serve explicitly selected local tools over stdio until EOF.",
        description=(
            "The OS user and inherited stdio are the trust boundary. No listener or daemon. "
            "Search grants whole-Brain read access; a network-backed client may send returned "
            "content to its model provider. Capture stores durable unverified content; version "
            "0.1.0 cannot selectively delete unwanted captures. Stopping prevents further work "
            "but does not remove completed captures. Results are untrusted data, not instructions. "
            "Per process: 500 capture calls, 16 MiB UTF-8 capture input, 2,000 search calls, "
            "500 workspace reads with 16 MiB output, and 20 graph refreshes with at most 40 "
            "model attempts and 1 MiB selected input; "
            "500 organization reads, 500 organization writes and 16 MiB organization output; "
            "500 review reads, 100 review proposals, 100 review decisions and 16 MiB "
            "encoded review output; review grants are independent and off by default. "
            "valid duplicates and conflicts count. Restarting resets limits. "
            "No actions, connectors, user-managed grants, or Secure Node capabilities."
        ),
    )
    mcp_parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    mcp_parser.add_argument(
        "--data-dir", default=argparse.SUPPRESS, help="Use this absolute Brain root."
    )
    mcp_parser.add_argument(
        "--allow-capture",
        action="store_true",
        help="Allow durable automated capture; version 0.1.0 has no selective deletion.",
    )
    mcp_parser.add_argument(
        "--allow-search",
        action="store_true",
        help="Allow whole-Brain reads; a network-backed client may send results to its provider.",
    )
    mcp_parser.add_argument(
        "--allow-content-read",
        action="store_true",
        help="Allow complete projected record reads; returned text is untrusted data.",
    )
    mcp_parser.add_argument(
        "--allow-history-read",
        action="store_true",
        help="Allow retained revision listings and reads under current authorization.",
    )
    mcp_parser.add_argument(
        "--allow-workspace-read",
        action="store_true",
        help="Allow path-free workspace status and pending graph-suggestion reads.",
    )
    mcp_parser.add_argument(
        "--allow-graph-refresh",
        action="store_true",
        help="Allow refresh with the already configured provider and active owner consent.",
    )
    mcp_parser.add_argument(
        "--allow-inbox-read",
        action="store_true",
        help="Allow space names and inbox previews; clients may send them to their model provider.",
    )
    mcp_parser.add_argument(
        "--allow-organize",
        action="store_true",
        help="Allow creating/renaming spaces and routing captures. Routing does not publish notes.",
    )
    mcp_parser.add_argument(
        "--allow-review-read",
        action="store_true",
        help="Allow bounded proposal listings and projected draft/evidence inspection.",
    )
    mcp_parser.add_argument(
        "--allow-review-propose",
        action="store_true",
        help="Allow durable proposals from explicitly selected routed captures.",
    )
    mcp_parser.add_argument(
        "--allow-review-decide",
        action="store_true",
        help="Allow digest-bound approve, reject, and edit-and-approve decisions.",
    )
    plugin_parser = subparsers.add_parser(
        "plugin",
        help=argparse.SUPPRESS,
        description="Serve one lifecycle-owned Open Brain desktop-plugin session over stdio.",
    )
    plugin_parser.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    agent_parser = subparsers.add_parser(
        "agent",
        help="Preview or apply owned Claude Code and Codex memory setup.",
    )
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_setup_parser = agent_subparsers.add_parser(
        "setup",
        help="Configure or remove one previewed agent integration.",
    )
    _add_local_options(agent_setup_parser)
    agent_setup_parser.add_argument("--client", required=True, choices=("claude-code", "codex"))
    agent_setup_parser.add_argument("--scope", required=True, choices=("project", "user"))
    agent_setup_parser.add_argument("--project-dir")
    agent_setup_parser.add_argument("--allow-capture", action="store_true")
    agent_setup_parser.add_argument("--allow-search", action="store_true")
    agent_setup_parser.add_argument("--allow-content-read", action="store_true")
    agent_setup_parser.add_argument("--allow-history-read", action="store_true")
    agent_setup_parser.add_argument("--allow-inbox-read", action="store_true")
    agent_setup_parser.add_argument("--allow-organize", action="store_true")
    agent_setup_parser.add_argument("--allow-review-read", action="store_true")
    agent_setup_parser.add_argument("--allow-review-propose", action="store_true")
    agent_setup_parser.add_argument("--allow-review-decide", action="store_true")
    agent_setup_parser.add_argument(
        "--action", choices=("configure", "remove"), default="configure"
    )
    agent_setup_parser.add_argument("--apply", action="store_true")
    agent_setup_parser.add_argument("--preview-id")
    agent_setup_parser.add_argument(
        "--runtime", help="Use this exact absolute runtime path for contributor setup."
    )
    obsidian_plugin_parser = subparsers.add_parser(
        "obsidian-plugin",
        help="Install, inspect, or remove the desktop Obsidian plugin.",
    )
    _add_local_options(obsidian_plugin_parser)
    obsidian_plugin_parser.add_argument(
        "action",
        choices=("install", "status", "remove"),
    )
    workspace_parser = subparsers.add_parser(
        "workspace", help="Manage the dedicated Open Brain Markdown workspace."
    )
    _add_local_options(workspace_parser)
    workspace_parser.add_argument(
        "action",
        choices=(
            "setup",
            "status",
            "observe",
            "refresh",
            "accept",
            "materialize",
            "deactivate",
            "restore",
            "resolve",
            "recover",
        ),
    )
    workspace_parser.add_argument("note_id", nargs="?")
    workspace_parser.add_argument("--generation", type=int)
    workspace_parser.add_argument("--choice", choices=("accepted", "candidate"))
    workspace_parser.add_argument("--operation-id")
    workspace_parser.add_argument("--after")
    workspace_parser.add_argument("--limit", type=int)
    workspace_parser.add_argument("--abandon", action="store_true")
    workspace_parser.add_argument("--expected-digest")
    workspace_parser.add_argument("--request-id")
    graph_parser = subparsers.add_parser(
        "graph", help="Review graph suggestions and configure semantic consent."
    )
    _add_local_options(graph_parser)
    graph_parser.add_argument(
        "action",
        choices=(
            "projection",
            "canvas",
            "refresh-structural",
            "suggestions",
            "accept",
            "grant-consent",
            "revoke-consent",
            "exclude",
            "include",
        ),
    )
    graph_parser.add_argument("subject", nargs="?")
    graph_parser.add_argument("--provider", choices=tuple(item.value for item in ManagedProvider))
    graph_parser.add_argument(
        "--access-mode", choices=tuple(item.value for item in ManagedAccessMode)
    )
    graph_parser.add_argument("--kind", choices=("note", "folder"))
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


def _is_workspace_recovery(parsed: argparse.Namespace) -> bool:
    return parsed.command == "workspace" and getattr(parsed, "action", None) == "recover"


def _validate_workspace_recovery_arguments(parsed: argparse.Namespace) -> None:
    action = getattr(parsed, "action", None)
    operation_id = getattr(parsed, "operation_id", None)
    after = getattr(parsed, "after", None)
    limit = getattr(parsed, "limit", None)
    abandon = bool(getattr(parsed, "abandon", False))
    expected_digest = getattr(parsed, "expected_digest", None)
    request_id = getattr(parsed, "request_id", None)
    recovery_flag_used = (
        operation_id is not None
        or after is not None
        or limit is not None
        or abandon
        or expected_digest is not None
        or request_id is not None
    )
    if action != "recover":
        if recovery_flag_used:
            raise ValueError("recovery flags require workspace recover")
        return
    if (
        getattr(parsed, "note_id", None) is not None
        or getattr(parsed, "generation", None) is not None
        or getattr(parsed, "choice", None) is not None
        or limit is not None
        and (type(limit) is not int or not 1 <= limit <= 100)
        or operation_id is not None
        and (after is not None or limit is not None)
    ):
        raise ValueError("invalid workspace recovery arguments")
    if abandon:
        if (
            operation_id is None
            or expected_digest is None
            or request_id is None
            or after is not None
            or limit is not None
        ):
            raise ValueError("incomplete workspace abandonment")
    elif expected_digest is not None or request_id is not None:
        raise ValueError("owner decision arguments require abandonment")


def _add_t03_parsers(
    subparsers: argparse._SubParsersAction[_RedactedArgumentParser],
) -> None:
    search_page = subparsers.add_parser(
        "search-page", help="Search one filtered, cursor-continuable result page."
    )
    _add_local_options(search_page)
    search_page.add_argument("query")
    search_page.add_argument("--space-id", action="append", dest="space_ids")
    search_page.add_argument("--payload-family", action="append", dest="payload_families")
    search_page.add_argument(
        "--record-type", action="append", choices=("source", "canonical"), dest="record_types"
    )
    search_page.add_argument(
        "--mode",
        choices=("lexical", "hybrid_preferred", "hybrid_required"),
        default="lexical",
    )
    search_page.add_argument("--limit", type=int, default=10)
    search_page.add_argument("--cursor")

    read = subparsers.add_parser("read", help="Read one complete projected record in chunks.")
    _add_local_options(read)
    read.add_argument("record_id")
    read.add_argument("--expected-revision-id", required=True)
    read.add_argument("--target-bytes", type=int, default=32_768)
    read.add_argument("--cursor")

    history = subparsers.add_parser("history", help="List or read retained record revisions.")
    _add_local_options(history)
    history_children = history.add_subparsers(dest="history_action", required=True)
    history_list = history_children.add_parser("list")
    _add_local_options(history_list)
    history_list.add_argument("record_id")
    history_list.add_argument("--limit", type=int, default=10)
    history_list.add_argument("--cursor")
    history_show = history_children.add_parser("show")
    _add_local_options(history_show)
    history_show.add_argument("record_id")
    history_show.add_argument("--expected-revision-id", required=True)
    history_show.add_argument("--target-bytes", type=int, default=32_768)
    history_show.add_argument("--cursor")

    source = subparsers.add_parser("source", help="Route a logical source with head/version CAS.")
    _add_local_options(source)
    source_children = source.add_subparsers(dest="source_action", required=True)
    source_route = source_children.add_parser("route")
    _add_local_options(source_route)
    source_route.add_argument("source_id")
    source_route.add_argument("space_id")
    source_route.add_argument("--expected-head", required=True)
    source_route.add_argument("--expected-route-version", required=True, type=int)
    source_route.add_argument("--operation-id", required=True)

    relationship = subparsers.add_parser(
        "relationship", help="Owner-only revision-bound relationship decisions and listing."
    )
    _add_local_options(relationship)
    relationship_children = relationship.add_subparsers(
        dest="relationship_action", required=True
    )
    relationship_decide = relationship_children.add_parser("decide")
    _add_local_options(relationship_decide)
    for side in ("left", "right"):
        relationship_decide.add_argument(f"--{side}-record-id", required=True)
        relationship_decide.add_argument(f"--{side}-revision-id", required=True)
    relationship_decide.add_argument(
        "--kind", required=True, choices=("duplicate_of", "supersedes", "contradicts")
    )
    relationship_decide.add_argument(
        "--decision", required=True, choices=("accept", "reject", "remove")
    )
    relationship_decide.add_argument(
        "--expected-relationship-version", required=True, type=int
    )
    relationship_decide.add_argument("--operation-id", required=True)
    relationship_list = relationship_children.add_parser("list")
    _add_local_options(relationship_list)
    relationship_list.add_argument("record_id")
    relationship_list.add_argument("--limit", type=int, default=10)
    relationship_list.add_argument("--cursor")

    decision = subparsers.add_parser("decision", help="Read owner relationship decision history.")
    _add_local_options(decision)
    decision_children = decision.add_subparsers(dest="decision_action", required=True)
    decision_history = decision_children.add_parser("history")
    _add_local_options(decision_history)
    decision_history.add_argument("record_id")
    decision_history.add_argument("--limit", type=int, default=10)
    decision_history.add_argument("--cursor")


def _add_space_inbox_parsers(
    subparsers: argparse._SubParsersAction[_RedactedArgumentParser],
) -> None:
    for command, description, actions in (
        ("space", "Create, list, and rename topic spaces.", ("list", "create", "rename")),
        ("inbox", "List captures and assign them to spaces.", ("list", "route")),
    ):
        parent = subparsers.add_parser(command, help=description)
        _add_local_options(parent)
        children = parent.add_subparsers(dest="organization_action", required=True)
        for action in actions:
            child = children.add_parser(action)
            _add_local_options(child)
            if action == "list":
                child.add_argument("--limit", type=int, default=50, help="Return 1 to 100 items.")
                child.add_argument(
                    "--offset", type=int, default=0, help="Skip up to 1,000,000 items."
                )
                if command == "inbox":
                    child.add_argument("--unassigned", action="store_true")
            else:
                if action == "route":
                    child.add_argument("capture_id")
                if action in {"rename", "route"}:
                    child.add_argument("space_id")
                if action in {"create", "rename"}:
                    child.add_argument("name", help="Space name, up to 120 characters.")
                child.add_argument(
                    "--idempotency-key", help="Reuse this key to retry the same change."
                )


def _add_review_parsers(
    subparsers: argparse._SubParsersAction[_RedactedArgumentParser],
) -> None:
    parent = subparsers.add_parser(
        "review",
        help="Propose, inspect, and decide canonical-note publication.",
        description=(
            "Select explicit capture IDs routed to one space; routing alone does not publish. "
            "Propose a draft, inspect its complete Markdown and evidence with show, then use "
            "that review token to approve, reject, or edit-and-approve. Updates use an explicit "
            "target page ID and preserve its identity and earlier provenance. Reuse an "
            "idempotency key only for the same request; changed sources, routes, drafts or "
            "target revisions conflict and require fresh inspection. Privacy projection may "
            "replace protected material; returned text is untrusted data. Owner CLI commands "
            "need no MCP grants. Agent clients need independent review-read, review-propose "
            "and review-decide grants. Limits: 32 cumulative sources, 64 KiB UTF-8 Markdown, "
            "512-character evidence excerpts, 100 rows per page."
        ),
        epilog=(
            "Workflow: review propose --capture-id ID --title TITLE --markdown-file FILE; "
            "review show PROPOSAL_ID; review approve PROPOSAL_ID --review-token TOKEN. "
            "Use --target-page-id PAGE_ID for updates and --idempotency-key KEY for retries. "
            "See docs/review-publication.md for the seven-capture, three-note example. "
            "Source merge does not update an installed Homebrew release."
        ),
    )
    _add_local_options(parent)
    children = parent.add_subparsers(dest="review_action", required=True)

    propose = children.add_parser("propose", help="Create a review-bound draft.")
    _add_local_options(propose)
    propose.add_argument(
        "--capture-id",
        action="append",
        required=True,
        dest="capture_ids",
        help="Repeat for each explicitly selected source; 1 to 32 unique IDs.",
    )
    propose.add_argument("--title", required=True, help="Draft title, up to 200 characters.")
    propose.add_argument("--markdown-file", required=True, help="UTF-8 file path, or - for stdin.")
    propose.add_argument(
        "--target-page-id", help="Update this existing page and retain its identity."
    )
    propose.add_argument("--idempotency-key", help="Reuse only to retry this exact request.")

    listing = children.add_parser("list", help="List bounded proposal summaries.")
    _add_local_options(listing)
    listing.add_argument("--capture-id")
    listing.add_argument("--space-id")
    listing.add_argument("--status", choices=("pending", "approved", "rejected", "edited"))
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)

    show = children.add_parser("show", help="Inspect one complete projected proposal.")
    _add_local_options(show)
    show.add_argument("proposal_id")

    for action in ("approve", "reject", "edit-and-approve"):
        decision = children.add_parser(action)
        _add_local_options(decision)
        decision.add_argument("proposal_id")
        decision.add_argument("--review-token", required=True)
        if action == "edit-and-approve":
            decision.add_argument("--markdown-file", required=True, help="UTF-8 file path, or -.")
        decision.add_argument("--idempotency-key")


def _space_inbox_arguments(parsed: argparse.Namespace) -> tuple[str, dict[str, object]]:
    operation = f"{parsed.command}_{parsed.organization_action}"
    arguments: dict[str, object] = {}
    for key in ("limit", "offset", "name", "space_id", "capture_id", "idempotency_key"):
        value = getattr(parsed, key, None)
        if value is not None:
            arguments[key] = value
    if parsed.command == "inbox" and parsed.organization_action == "list":
        arguments["unassigned_only"] = parsed.unassigned
    return operation, arguments


def _run_space_inbox(
    parsed: argparse.Namespace, service: SpaceInboxService, *, json_output: bool
) -> int:
    operation, arguments = _space_inbox_arguments(parsed)
    handlers = {
        "space_list": service.space_list,
        "space_create": service.space_create,
        "space_rename": service.space_rename,
        "inbox_list": service.inbox_list,
        "inbox_route": service.inbox_route,
    }
    result = handlers[operation](arguments)
    if json_output:
        _write_json(result)
    elif operation.endswith("_list"):
        rows = cast(list[dict[str, object]], result.get("items", result.get("spaces", [])))
        if not rows:
            print("No matching captures." if operation == "inbox_list" else "No spaces yet.")
        for row in rows:
            if operation == "space_list":
                print(_terminal_text(f"{row['space_id']}  {row['name']}"))
            else:
                label = row.get("title") or row.get("preview") or row["payload_family"]
                print(
                    _terminal_text(
                        f"{row['capture_id']}  [{row['space_id'] or 'unassigned'}]  {label}"
                    )
                )
        if result["next_offset"] is not None:
            print(f"More results: repeat with --offset {result['next_offset']}.")
        if result.get("offset_limit_reached"):
            print("The listing offset limit was reached; additional records were not listed.")
    elif operation == "inbox_route":
        print(f"Routed {result['capture_id']} to {result['space_id']}.")
    else:
        space = cast(dict[str, object], result["space"])
        print(_terminal_text(f"Space {result['status']}: {space['space_id']}  {space['name']}"))
    return 0


def _write_space_inbox_failure(code: str, *, json_output: bool) -> int:
    if code == "invalid_arguments":
        _write_usage_failure(json_output=json_output)
        return 2
    messages = {
        "idempotency_conflict": "This retry key belongs to a different change. Use a new key.",
        "unknown_space": "Space not found. Run open-brain space list.",
        "unknown_route_target": "Capture or space not found. List the inbox and spaces again.",
        "published_capture": "Published captures cannot be rerouted.",
    }
    message = messages.get(code, "Open Brain could not complete the organization command.")
    if json_output:
        _write_json({"status": "failed", "error": {"code": code, "message": message}})
    else:
        print(message, file=sys.stderr)
    return 1


def _review_arguments(parsed: argparse.Namespace) -> tuple[str, dict[str, object]]:
    operation = cast(str, parsed.review_action).replace("-", "_")
    arguments: dict[str, object] = {}
    if operation == "propose":
        arguments.update(
            capture_ids=parsed.capture_ids,
            title=parsed.title,
            markdown=_read_review_markdown(parsed.markdown_file),
        )
        if parsed.target_page_id is not None:
            arguments["target_page_id"] = parsed.target_page_id
    elif operation == "list":
        for field in ("capture_id", "space_id", "status"):
            value = getattr(parsed, field)
            if value is not None:
                arguments[field] = value
        arguments.update(limit=parsed.limit, offset=parsed.offset)
    elif operation == "show":
        arguments["proposal_id"] = parsed.proposal_id
    else:
        arguments.update(proposal_id=parsed.proposal_id, review_token=parsed.review_token)
        if operation == "edit_and_approve":
            arguments["markdown"] = _read_review_markdown(parsed.markdown_file)
    key = getattr(parsed, "idempotency_key", None)
    if key is not None:
        arguments["idempotency_key"] = key
    return operation, arguments


def _read_review_markdown(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReviewPublicationError("invalid_arguments")
    try:
        if value == "-":
            payload = sys.stdin.buffer.read(MAX_REVIEW_MARKDOWN_BYTES + 1)
        else:
            path = Path(value)
            if not path.is_file():
                raise ReviewPublicationError("invalid_arguments")
            with path.open("rb") as source:
                payload = source.read(MAX_REVIEW_MARKDOWN_BYTES + 1)
    except OSError:
        raise ReviewPublicationError("invalid_arguments") from None
    if len(payload) > MAX_REVIEW_MARKDOWN_BYTES:
        raise ReviewPublicationError("invalid_arguments")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ReviewPublicationError("invalid_arguments") from None


def _run_review(
    parsed: argparse.Namespace,
    service: ReviewPublicationService,
    *,
    json_output: bool,
) -> int:
    operation = cast(str, parsed.review_action).replace("-", "_")
    arguments = cast(dict[str, object], parsed.review_arguments)
    handlers = {
        "propose": service.propose,
        "list": service.list,
        "show": service.show,
        "approve": service.approve,
        "reject": service.reject,
        "edit_and_approve": service.edit_and_approve,
    }
    result = handlers[operation](arguments)
    if json_output:
        _write_json(result)
    elif operation == "list":
        rows = cast(list[dict[str, object]], result["proposals"])
        if not rows:
            print("No matching review proposals.")
        for row in rows:
            print(
                _terminal_text(
                    f"{row['proposal_id']}  [{row['status']}]  {row['operation']}  {row['title']}"
                )
            )
        if result["next_offset"] is not None:
            print(f"More results: repeat with --offset {result['next_offset']}.")
    elif operation == "show":
        print(_terminal_text(f"Proposal {result['proposal_id']} [{result['proposal_status']}]"))
        print(_terminal_text(f"Review token: {result['review_token']}"))
        if result.get("projection_applied"):
            print("Privacy projection replaced protected material.")
        print(_terminal_markdown(cast(str, result["markdown"])))
    else:
        print(_terminal_text(json.dumps(result, ensure_ascii=False, sort_keys=True)))
    return 0


def _write_review_failure(code: str, *, json_output: bool) -> int:
    if code == "invalid_arguments":
        _write_usage_failure(json_output=json_output)
        return 2
    messages = {
        "idempotency_conflict": "This retry key belongs to a different review operation.",
        "unknown_proposal": "Review proposal not found. List proposals again.",
        "unknown_capture": "One or more selected captures were not found.",
        "unknown_page": "The target canonical page was not found.",
        "duplicate_source": "Each selected capture ID must be unique.",
        "mixed_source_spaces": "All selected captures must be routed to the same space.",
        "source_unrouted": "Every selected capture must be routed before proposing publication.",
        "terminal_decision": "This proposal already has a terminal decision.",
        "review_conflict": "The reviewed source, route, draft, or target changed. Inspect again.",
        "response_too_large": "The complete projected proposal exceeds the inspection limit.",
    }
    message = messages.get(code, "Open Brain could not complete the review command.")
    if json_output:
        _write_json({"status": "failed", "error": {"code": code, "message": message}})
    else:
        print(message, file=sys.stderr)
    return 1


def _run_agent_setup(
    parsed: argparse.Namespace,
    *,
    selection: object,
    environment: Mapping[str, object],
    json_output: bool,
) -> int:
    from open_brain.local_data import LocalRootSelection

    if not isinstance(selection, LocalRootSelection) or parsed.agent_command != "setup":
        raise AgentSetupFailure("invalid_arguments")
    if not parsed.apply and parsed.preview_id is not None:
        raise AgentSetupFailure("invalid_arguments")
    runtime_path = resolve_agent_runtime(parsed.runtime)
    values = {
        "action": parsed.action,
        "allow_capture": parsed.allow_capture,
        "allow_search": parsed.allow_search,
        "allow_content_read": parsed.allow_content_read,
        "allow_history_read": parsed.allow_history_read,
        "allow_inbox_read": parsed.allow_inbox_read,
        "allow_organize": parsed.allow_organize,
        "allow_review_read": parsed.allow_review_read,
        "allow_review_propose": parsed.allow_review_propose,
        "allow_review_decide": parsed.allow_review_decide,
        "client": parsed.client,
        "environment": environment,
        "project_dir": parsed.project_dir,
        "runtime_path": runtime_path,
        "scope": parsed.scope,
    }
    result = (
        apply_agent_setup(selection, preview_id=parsed.preview_id, **values)
        if parsed.apply
        else preview_agent_setup(selection, **values)
    )
    if json_output:
        _write_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _t03_cli_request(parsed: argparse.Namespace) -> tuple[str, dict[str, object]]:
    operation: str
    arguments: dict[str, object] = {"dto_version": 1}
    if parsed.command == "search-page":
        operation = "search.page"
        arguments.update(
            query=parsed.query,
            filters={
                "space_ids": sorted(set(parsed.space_ids or [])),
                "payload_families": sorted(set(parsed.payload_families or [])),
                "record_types": sorted(set(parsed.record_types or [])),
            },
            mode=parsed.mode,
            limit=parsed.limit,
            cursor=parsed.cursor,
        )
    elif parsed.command == "read":
        operation = "record.read"
        arguments.update(
            record_id=parsed.record_id,
            expected_revision_id=parsed.expected_revision_id,
            target_bytes=parsed.target_bytes,
            cursor=parsed.cursor,
        )
    elif parsed.command == "history":
        operation = "history.list" if parsed.history_action == "list" else "history.show"
        arguments.update(record_id=parsed.record_id, cursor=parsed.cursor)
        if parsed.history_action == "list":
            arguments["limit"] = parsed.limit
        else:
            arguments.update(
                expected_revision_id=parsed.expected_revision_id,
                target_bytes=parsed.target_bytes,
            )
    elif parsed.command == "source":
        operation = "source.route"
        arguments.update(
            source_id=parsed.source_id,
            space_id=parsed.space_id,
            expected_head=parsed.expected_head,
            expected_route_version=parsed.expected_route_version,
            operation_id=parsed.operation_id,
        )
    elif parsed.command == "relationship" and parsed.relationship_action == "decide":
        operation = "relationship.decide"
        arguments.update(
            left={
                "record_id": parsed.left_record_id,
                "revision_id": parsed.left_revision_id,
            },
            right={
                "record_id": parsed.right_record_id,
                "revision_id": parsed.right_revision_id,
            },
            kind=parsed.kind,
            decision=parsed.decision,
            expected_relationship_version=parsed.expected_relationship_version,
            operation_id=parsed.operation_id,
        )
    elif parsed.command == "relationship":
        operation = "relationship.list"
        arguments.update(record_id=parsed.record_id, limit=parsed.limit, cursor=parsed.cursor)
    elif parsed.command == "decision":
        operation = "decision.history"
        arguments.update(record_id=parsed.record_id, limit=parsed.limit, cursor=parsed.cursor)
    else:
        raise T03AppError("invalid_arguments")
    return operation, arguments


def _run_t03_cli(
    parsed: argparse.Namespace,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
) -> int:
    operation, arguments = _t03_cli_request(parsed)
    adapter = T03AppAdapter(
        tasks,
        owner_authority(tasks, session_id="cli-" + str(uuid.uuid4())),
        frozenset({"search", "content-read", "history-read", "organize"}),
        owner=True,
    )
    result = adapter.invoke(operation, arguments)
    if json_output:
        _write_json(result)
    elif operation in {"record.read", "history.show"}:
        content = cast(dict[str, object], result["content"])
        print(_terminal_markdown(cast(str, content["text"])))
        if result["next_cursor"] is not None:
            print(_terminal_text(f"Next cursor: {result['next_cursor']}"))
    else:
        print(_terminal_markdown(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)))
    return 0


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
    if parsed.command in {
        "search-page",
        "read",
        "history",
        "source",
        "relationship",
        "decision",
    }:
        return _run_t03_cli(parsed, tasks, json_output=json_output)
    if parsed.command in {"space", "inbox"}:
        return _run_space_inbox(parsed, SpaceInboxService(tasks.spaces), json_output=json_output)
    if parsed.command == "review":
        return _run_review(parsed, ReviewPublicationService(tasks.review), json_output=json_output)
    if parsed.command == "obsidian-plugin":
        return _run_obsidian_plugin(parsed, session, json_output=json_output)
    if parsed.command == "capture":
        capture_receipt = capture_text(
            tasks.capture,
            cast(str, parsed.text),
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
        results = search_brain(
            tasks.retrieval,
            tasks.reconciliation,
            cast(str, parsed.query),
            limit=cast(int, parsed.limit),
        )
        _write_search(results, json_output=json_output)
        return 0
    if parsed.command == "mcp":
        from open_brain.services.local_mcp import MAX_MESSAGE_BYTES, LocalMcpAdapter
        from open_brain.services.mcp_protocol import serve_stdio_mcp

        retrieval, reconciliation = tasks.retrieval, tasks.reconciliation

        def search(query: str, limit: int) -> tuple[RetrievalResult, ...]:
            return search_brain(retrieval, reconciliation, query, limit=limit)

        organization = SpaceInboxService(tasks.spaces)
        review = ReviewPublicationService(tasks.review)
        negotiated_grants = frozenset(
            grant
            for enabled, grant in (
                (parsed.allow_search, "search"),
                (parsed.allow_content_read, "content-read"),
                (parsed.allow_history_read, "history-read"),
                (parsed.allow_organize, "organize"),
            )
            if enabled
        )
        negotiated_candidate = (
            T03AppAdapter(
                tasks,
                agent_authority(
                    principal_id="open-brain-mcp-local",
                    session_id="mcp-" + str(uuid.uuid4()),
                    grants=negotiated_grants,
                ),
                negotiated_grants,
            )
            if negotiated_grants
            else None
        )
        negotiated = negotiated_candidate
        adapter = LocalMcpAdapter(
            capture=mcp_capture_sink(tasks) if parsed.allow_capture else None,
            search=search if parsed.allow_search else None,
            inbox_list=organization.inbox_list if parsed.allow_inbox_read else None,
            space_list=organization.space_list if parsed.allow_inbox_read else None,
            space_create=organization.space_create if parsed.allow_organize else None,
            space_rename=organization.space_rename if parsed.allow_organize else None,
            inbox_route=organization.inbox_route if parsed.allow_organize else None,
            review_list=review.list if parsed.allow_review_read else None,
            review_show=review.show if parsed.allow_review_read else None,
            review_propose=review.propose if parsed.allow_review_propose else None,
            review_approve=review.approve if parsed.allow_review_decide else None,
            review_reject=review.reject if parsed.allow_review_decide else None,
            review_edit_and_approve=(
                review.edit_and_approve if parsed.allow_review_decide else None
            ),
            negotiated=negotiated,
            workspace_status=(
                (lambda: workspace_status_result(tasks)) if parsed.allow_workspace_read else None
            ),
            graph_suggestions=(
                (lambda: graph_suggestions(tasks)) if parsed.allow_workspace_read else None
            ),
            graph_projection=(
                (lambda: graph_projection(tasks)) if parsed.allow_workspace_read else None
            ),
            graph_refresh=(
                (
                    lambda _remaining_attempts, _remaining_bytes: (
                        {"reason": "provider_not_configured", "status": "unavailable"},
                        0,
                        0,
                    )
                )
                if parsed.allow_graph_refresh
                else None
            ),
        )
        serve_stdio_mcp(
            adapter,
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
            maximum_message_bytes=MAX_MESSAGE_BYTES,
        )
        return 0
    if parsed.command == "workspace":
        return _run_workspace(parsed, session, tasks, json_output=json_output)
    if parsed.command == "graph":
        return _run_graph(parsed, tasks, json_output=json_output)
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


def _run_obsidian_plugin(
    parsed: argparse.Namespace,
    session: LocalBrainSession,
    *,
    json_output: bool,
) -> int:
    from open_brain.services.obsidian_plugin import (
        ObsidianPluginFailure,
        discover_obsidian_plugin_assets,
        install_obsidian_plugin,
        load_obsidian_plugin_bundle,
        obsidian_plugin_status,
        remove_obsidian_plugin,
    )

    status = session.tasks.managed_workspace.status()
    if status is None or not status.connected:
        failure = ObsidianPluginFailure("workspace_unconfigured")
        return _write_obsidian_plugin_failure(failure.code, json_output=json_output)
    workspace = session.profile.root.parent / "Open Brain Vault"
    try:
        action = cast(str, parsed.action)
        if action == "remove":
            payload = remove_obsidian_plugin(workspace)
        else:
            bundle = load_obsidian_plugin_bundle(discover_obsidian_plugin_assets())
            payload = (
                install_obsidian_plugin(workspace, bundle)
                if action == "install"
                else obsidian_plugin_status(workspace, bundle)
            )
    except ObsidianPluginFailure as error:
        return _write_obsidian_plugin_failure(error.code, json_output=json_output)
    _write_managed(payload, json_output=json_output)
    return 0


def _run_workspace(
    parsed: argparse.Namespace,
    session: LocalBrainSession,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
) -> int:
    action = cast(str, parsed.action)
    if action == "status":
        _write_managed(workspace_status_result(tasks), json_output=json_output)
        return 0
    operation_id = "operation.cli." + str(uuid.uuid4())
    if action == "setup":
        workspace = session.profile.root.parent / "Open Brain Vault"
        try:
            metadata = workspace.lstat()
        except FileNotFoundError:
            workspace.mkdir(mode=0o700)
        else:
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("managed workspace destination is unsafe")
        receipt = tasks.managed_workspace.setup(str(workspace), operation_id=operation_id)
        _write_managed(_workspace_receipt(receipt), json_output=json_output)
        return 0
    status = tasks.managed_workspace.status()
    if status is None:
        raise ValueError("managed workspace is not configured")
    if action == "observe":
        observation = tasks.managed_workspace.observe(status.workspace_id)
        _write_managed(
            {
                "generation": observation.generation,
                "notes": [
                    {
                        "changed": note.changed,
                        "note_id": note.note_id,
                        "present": note.present,
                        "relative_path": note.relative_path,
                    }
                    for note in observation.notes
                ],
                "status": "observed",
                "workspace_id": observation.workspace_id,
            },
            json_output=json_output,
        )
        return 0
    if action == "refresh":
        receipt = tasks.managed_workspace.refresh(status.workspace_id, operation_id=operation_id)
        _write_managed(_workspace_receipt(receipt), json_output=json_output)
        return 0
    note_id = getattr(parsed, "note_id", None)
    if not isinstance(note_id, str):
        raise ValueError("managed note identity is required")
    if action == "accept":
        generation = getattr(parsed, "generation", None)
        if type(generation) is not int:
            raise ValueError("managed observation generation is required")
        receipt = tasks.managed_workspace.accept_observed(
            status.workspace_id,
            note_id,
            generation=generation,
            operation_id=operation_id,
        )
    elif action == "materialize":
        receipt = tasks.managed_workspace.materialize(
            status.workspace_id, note_id, operation_id=operation_id
        )
    elif action == "deactivate":
        receipt = tasks.managed_workspace.deactivate(
            status.workspace_id, note_id, operation_id=operation_id
        )
    elif action == "restore":
        receipt = tasks.managed_workspace.restore(
            status.workspace_id, note_id, operation_id=operation_id
        )
    elif action == "resolve":
        choice = getattr(parsed, "choice", None)
        if choice not in {"accepted", "candidate"}:
            raise ValueError("managed conflict choice is required")
        receipt = tasks.managed_workspace.resolve_conflict(
            status.workspace_id,
            note_id,
            "workspace" if choice == "candidate" else "accepted",
            operation_id=operation_id,
        )
    else:
        raise ValueError("invalid managed workspace action")
    _write_managed(_workspace_receipt(receipt), json_output=json_output)
    return 0


def _run_graph(parsed: argparse.Namespace, tasks: EngineTaskSet, *, json_output: bool) -> int:
    action = cast(str, parsed.action)
    if action == "canvas":
        _write_managed(graph_canvas(tasks), json_output=json_output)
        return 0
    if action == "projection":
        _write_managed(graph_projection(tasks), json_output=json_output)
        return 0
    if action == "refresh-structural":
        _write_managed(refresh_structural_graph(tasks), json_output=json_output)
        return 0
    if action == "suggestions":
        _write_managed(graph_suggestions(tasks), json_output=json_output)
        return 0
    status = tasks.managed_workspace.status()
    if status is None:
        raise ValueError("managed workspace is not configured")
    subject = getattr(parsed, "subject", None)
    operation_id = "operation.cli." + str(uuid.uuid4())
    if action == "accept":
        if not isinstance(subject, str):
            raise ValueError("managed suggestion identity is required")
        receipt = tasks.managed_inference.accept_suggestion(
            status.workspace_id, subject, operation_id=operation_id
        )
        payload = {
            "duplicate": receipt.duplicate,
            "request_id": receipt.request_id,
            "status": receipt.status,
            "suggestion_id": receipt.suggestion_id,
        }
    elif action in {"grant-consent", "revoke-consent"}:
        provider = getattr(parsed, "provider", None)
        access_mode = getattr(parsed, "access_mode", None)
        if not isinstance(provider, str) or not isinstance(access_mode, str):
            raise ValueError("managed provider and access mode are required")
        operation = (
            tasks.managed_policy.grant_consent
            if action == "grant-consent"
            else tasks.managed_policy.revoke_consent
        )
        policy_receipt = operation(
            status.workspace_id,
            ManagedProvider(provider),
            ManagedAccessMode(access_mode),
            operation_id=operation_id,
        )
        payload = {
            "duplicate": policy_receipt.duplicate,
            "policy_generation": policy_receipt.policy_generation,
            "status": policy_receipt.status,
            "workspace_id": policy_receipt.workspace_id,
        }
    elif action in {"exclude", "include"}:
        kind = getattr(parsed, "kind", None)
        if not isinstance(kind, str) or not isinstance(subject, str):
            raise ValueError("managed exclusion kind and subject are required")
        policy_receipt = tasks.managed_policy.set_exclusion(
            status.workspace_id,
            kind,
            subject,
            excluded=action == "exclude",
            operation_id=operation_id,
        )
        payload = {
            "duplicate": policy_receipt.duplicate,
            "policy_generation": policy_receipt.policy_generation,
            "status": policy_receipt.status,
            "workspace_id": policy_receipt.workspace_id,
        }
    else:
        raise ValueError("invalid managed graph action")
    _write_managed(payload, json_output=json_output)
    return 0


def _workspace_receipt(receipt: ManagedWorkspaceReceipt) -> dict[str, object]:
    return {
        "duplicate": receipt.duplicate,
        "generation": receipt.generation,
        "note_id": receipt.note_id,
        "status": receipt.status,
        "workspace_id": receipt.workspace_id,
    }


def _write_managed(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        _write_json(payload)
    else:
        print(_terminal_text(json.dumps(payload, sort_keys=True)))


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
    payload = capture_result(receipt)
    if json_output:
        _write_json(payload)
    else:
        print(f"Captured {receipt.capture_id}")


def _write_search(results: tuple[RetrievalResult, ...], *, json_output: bool) -> None:
    payload = search_result(results)
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
        "schema_version": receipt.schema_version,
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
    payload = {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
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
            "Daemon running: false. "
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
        "foreground-runtime": _foreground_runtime_is_safe,
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


def _foreground_runtime_is_safe(session: LocalBrainSession) -> bool:
    session.prepared.revalidate()
    return True


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


def _terminal_markdown(value: str) -> str:
    return "".join(
        character
        if character in {"\n", "\t"}
        or (
            ord(character) >= 32
            and not 0x7F <= ord(character) <= 0x9F
            and unicodedata.category(character) != "Cf"
        )
        else " "
        for character in value
    )


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
    try:
        manifest_value = json.loads(manifest)
        manifest_version = cast(dict[str, object], manifest_value)["schema_version"]
    except UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError:
        raise ValueError("Portable export manifest is unavailable") from None
    if type(manifest_version) is not int or manifest_version not in {1, 2, 3}:
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
                "schema_version": manifest_version,
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
            or value["schema_version"] not in {1, 2, 3}
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


def _write_obsidian_plugin_failure(code: str, *, json_output: bool) -> int:
    messages = {
        "assets_unavailable": "The packaged Open Brain Obsidian plugin is unavailable.",
        "foreign_plugin": "The Obsidian plugin directory is not owned by Open Brain.",
        "modified_plugin": "An installed Open Brain plugin file was modified; no files changed.",
        "unsafe_workspace": "The managed Open Brain vault is unavailable or unsafe.",
        "workspace_unconfigured": (
            "Set up the managed Open Brain vault before installing the plugin."
        ),
    }
    message = messages.get(code, "Open Brain could not manage the Obsidian plugin.")
    if json_output:
        _write_json({"error": {"code": code, "message": message}, "status": "failed"})
    else:
        print(f"{code}: {message}", file=sys.stderr)
    return 78


def _write_agent_setup_failure(code: str, *, json_output: bool) -> int:
    messages = {
        "client_config_invalid": "The client configuration is malformed or unsupported.",
        "invalid_arguments": "The agent setup request is invalid.",
        "operation_failed": "Open Brain could not apply the agent setup transaction.",
        "setup_conflict": "An owned setup fragment changed or the target name is already in use.",
        "setup_preview_stale": "The setup preview is stale; create a new preview before applying.",
        "unsafe_config_path": "The client configuration path is unavailable or unsafe.",
    }
    message = messages[code]
    if json_output:
        _write_json({"error": {"code": code, "message": message}, "status": "failed"})
    else:
        print(f"{code}: {message}", file=sys.stderr)
    return 2 if code == "invalid_arguments" else 78


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


def _write_database_busy(*, json_output: bool) -> None:
    message = "The local Brain is busy. Retry the command."
    if json_output:
        _write_json({"error": {"code": "database_busy", "message": message}, "status": "failed"})
    else:
        print("database_busy: " + message, file=sys.stderr)


def _write_managed_recovery_failure(code: str, *, schema_upgraded: bool, json_output: bool) -> int:
    message = "Managed recovery could not complete safely."
    if json_output:
        _write_json(
            {
                "error": {"code": code, "message": message},
                "schema_upgraded": schema_upgraded,
                "status": "failed",
            }
        )
    else:
        print(f"{code}: {message}", file=sys.stderr)
    return 75 if code in {"database_busy", "runtime_in_use"} else 78


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
