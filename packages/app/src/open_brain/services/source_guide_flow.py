"""Foreground guide coordinator: one named decision, one bounded phase (P2 capture).

`advance` validates a closed decision, binds it to the private workflow journal
and performs exactly one action. Writes to collector state (configure, preview,
import) first persist an owner-visible intent, then run only after a matching
confirmation, under the journal's operation-lifetime guard and with the exact
saved request bytes. Lost acknowledgements stop advancement until an explicit
`reconcile`: import replays its exact saved request; configure and preview are
halt-only. Nothing here enables collection, resets a source, retries, or
fetches/imports a next page. Interactive prompts build the same decisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from open_brain.services.source_guide_adapter import (
    CollectorExecutable,
    GuideAdapterError,
    collector_handshake,
    configure_source,
    import_preview,
    list_accounts,
    preview_source,
    read_guide_metadata,
    read_resource_inventory,
    resolve_captured_record,
    resolve_collector_executable,
)
from open_brain.services.source_guide_journal import (
    JournalError,
    WorkflowJournal,
    canonical_json,
    new_journal,
    operation_record,
    strict_json_object,
)

if TYPE_CHECKING:
    from open_brain.services.source_guide import GuidePaths

__all__ = [
    "CONFIRMED_ACTIONS",
    "Decision",
    "GuideFlow",
    "configured_source",
    "parse_decision",
    "read_decision_file",
    "result_envelope",
]

_MAX_DECISION_BYTES: Final = 65_536
_MAX_BODY_BYTES: Final = 3_500_000
_MAX_READ_CHUNKS: Final = 128
_MAX_RETAINED: Final = 1000
_PAGE_RECEIPTS: Final = 25
_EXECUTION_RESERVE: Final = 24_576
CONFIRMED_ACTIONS: Final = frozenset(
    {"configure", "preview", "import", "acknowledge-inspection", "reconcile"}
)
_ARGUMENTS: Final[dict[str, frozenset[str]]] = {
    "start": frozenset(),
    "preflight": frozenset(
        {
            "client_kind",
            "scope",
            "context_path",
            "consent_state",
            "policy_path",
            "runtime_path",
            "provider_id",
            "client_version",
            "launch_context",
        }
    ),
    "accounts": frozenset(),
    "resources": frozenset({"connection_id", "cursor"}),
    "choose": frozenset(
        {"connection_id", "resource_id", "resource_type", "date_floor", "account_alias"}
    ),
    "configure": frozenset(),
    "preview": frozenset(),
    "import": frozenset({"preview_id"}),
    "inspect": frozenset({"receipt_id"}),
    "acknowledge-inspection": frozenset({"receipt_id", "inspection_sha256"}),
    "reconcile": frozenset({"operation_sequence"}),
    "cancel": frozenset(),
}
# Later-phase actions are valid decision words but are not available in this build.
_LATER: Final = frozenset(
    {
        "destination",
        "create-destination",
        "route",
        "consent",
        "select",
        "release-preview",
        "approve",
        "policy-preview",
        "policy-apply",
        "setup-preview",
        "setup-apply",
    }
)
# Configure and preview have no operation receipt: an ambiguous outcome halts.
_HALT_ONLY: Final = frozenset({"configure", "preview"})


class _Stop(Exception):
    def __init__(
        self,
        code: str,
        *,
        outcome: str = "refused",
        document: Mapping[str, object] | None = None,
        data: Mapping[str, object] | None = None,
        next_actions: Sequence[str] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.document = document
        self.data = data
        self.next_actions = tuple(next_actions)


@dataclass(frozen=True, slots=True)
class Decision:
    workflow_id: str
    expected_revision: int
    action: str
    confirmation: dict[str, str] | None
    arguments: dict[str, object]


def result_envelope(
    action: str,
    outcome: str,
    code: str | None = None,
    *,
    document: Mapping[str, object] | None = None,
    workflow_id: str | None = None,
    intent: Mapping[str, object] | None = None,
    data: Mapping[str, object] | None = None,
    next_actions: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": document["workflow_id"] if document is not None else workflow_id,
        "revision": document["revision"] if document is not None else None,
        "action": action,
        "outcome": outcome,
        "code": code,
        "intent": None if intent is None else dict(intent),
        "data": None if data is None else dict(data),
        "next_actions": list(next_actions)[:5],
        "retry_after_epoch": None,
    }


def _identifier(value: object, maximum: int = 512) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and not value.startswith("-")
        and not any(ord(item) < 32 or 127 <= ord(item) < 160 for item in value)
    )


def _short_text(value: object, maximum: int) -> bool:
    return _identifier(value, maximum)


def _absolute(value: object) -> bool:
    return (
        type(value) is str
        and _identifier(value, 4096)
        and Path(value).is_absolute()
        and ".." not in Path(value).parts
        and os.path.normpath(value) == value
    )


def _hex64(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


def _aware_timestamp(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


_LAUNCH_KEYS: Final = frozenset(
    {"surface", "cwd", "profile", "setting_sources", "settings_file", "overrides", "managed_state"}
)


def _arguments_ok(action: str, values: dict[str, object]) -> bool:
    if action == "preflight":
        launch = values["launch_context"]
        return (
            values["client_kind"] in {"claude-code", "codex"}
            and values["scope"] in {"project", "user"}
            and all(
                _absolute(values[key])
                for key in ("context_path", "consent_state", "policy_path", "runtime_path")
            )
            and _identifier(values["provider_id"])
            and _short_text(values["client_version"], 80)
            and isinstance(launch, dict)
            and set(launch) == _LAUNCH_KEYS
            and launch["surface"] == "standalone-cli"
            and launch["cwd"] == values["context_path"]
            and (launch["profile"] is None or _short_text(launch["profile"], 64))
            and launch["setting_sources"] == ["user", "project", "local"]
            and launch["settings_file"] is None
            and launch["overrides"] == []
            and launch["managed_state"] == "owner-declared-absent"
        )
    if action == "resources":
        return _identifier(values["connection_id"]) and (
            values["cursor"] is None or _identifier(values["cursor"], 8192)
        )
    if action == "choose":
        return (
            _identifier(values["connection_id"])
            and _identifier(values["resource_id"])
            and values["resource_type"] == "channel"
            and _aware_timestamp(values["date_floor"])
            and (values["account_alias"] is None or _short_text(values["account_alias"], 80))
        )
    if action == "import":
        return _identifier(values["preview_id"])
    if action == "inspect":
        return _identifier(values["receipt_id"])
    if action == "acknowledge-inspection":
        return _identifier(values["receipt_id"]) and _hex64(values["inspection_sha256"])
    if action == "reconcile":
        sequence = values["operation_sequence"]
        return type(sequence) is int and sequence >= 0
    return True


def parse_decision(value: object) -> Decision:
    """Validate the closed decision envelope and the closed per-action arguments."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "workflow_id",
        "expected_revision",
        "action",
        "confirmation",
        "arguments",
    }:
        raise _Stop("invalid_request")
    action, arguments = value["action"], value["arguments"]
    confirmation = value["confirmation"]
    revision = value["expected_revision"]
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not _identifier(value["workflow_id"])
        or type(revision) is not int
        or revision < 0
        or type(action) is not str
        or not isinstance(arguments, dict)
    ):
        raise _Stop("invalid_request")
    if action in _LATER:
        raise _Stop("invalid_request")
    if action not in _ARGUMENTS or set(arguments) != _ARGUMENTS[action]:
        raise _Stop("invalid_request")
    if confirmation is not None and (
        action not in CONFIRMED_ACTIONS
        or not isinstance(confirmation, dict)
        or set(confirmation) != {"intent_sha256", "decision"}
        or not _hex64(confirmation["intent_sha256"])
        or confirmation["decision"] != "approve"
    ):
        raise _Stop("invalid_request")
    if not _arguments_ok(action, cast(dict[str, object], arguments)):
        raise _Stop("invalid_request")
    return Decision(
        workflow_id=cast(str, value["workflow_id"]),
        expected_revision=revision,
        action=action,
        confirmation=None if confirmation is None else cast(dict[str, str], dict(confirmation)),
        arguments=cast(dict[str, object], dict(arguments)),
    )


def read_decision_file(path: Path) -> Decision:
    """An owned regular no-follow 0600 file under an owned 0700 parent, at most 64 KiB."""
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.fstat(parent)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            raw = os.read(fd, _MAX_DECISION_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        raise _Stop("invalid_request") from None
    if (
        directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) & 0o077
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or len(raw) > _MAX_DECISION_BYTES
    ):
        raise _Stop("invalid_request")
    try:
        return parse_decision(strict_json_object(raw))
    except ValueError, UnicodeError, RecursionError:
        raise _Stop("invalid_request") from None


def _executable_binding(executable: CollectorExecutable) -> dict[str, object]:
    return {
        "path": os.fspath(executable.path),
        "sha256": executable.sha256,
        "device": executable.device,
        "inode": executable.inode,
    }


def _collector_binding(paths: GuidePaths) -> str:
    value = {
        "brain_root": os.fspath(paths.brain),
        "collector_state": os.fspath(paths.state),
        "live_root": os.fspath(paths.state.parent / "live"),
    }
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def brain_identity(brain: Path) -> tuple[str, int]:
    """Read the existing Brain ID and issuer epoch read-only, without bootstrap."""
    from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
    from open_brain_engine.storage.sqlite import connect_database_read_only

    from open_brain.profile import open_existing_single_user_local

    try:
        profile = open_existing_single_user_local(brain)
        connection = connect_database_read_only(
            root=profile.root,
            database_name=PHASE1_STATE_DATABASE,
            expected_root_identity=profile.root_identity,
        )
        try:
            row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
        finally:
            connection.close()
    except Exception:
        raise _Stop("state_uninitialized") from None
    if row is None or type(row[0]) is not str or type(row[1]) is not int:
        raise _Stop("state_uninitialized")
    return row[0], row[1]


def configured_source(document: Mapping[str, object]) -> str | None:
    """The selected collector source is the latest completed configure result."""
    for operation in reversed(cast(list[dict[str, object]], document["operations"])):
        if operation["action"] == "configure" and operation["state"] == "completed":
            evidence = cast(dict[str, object], operation["evidence"])
            return cast(str, evidence["source_id"])
    return None


def _progress(
    phase: str,
    *,
    source_id: str | None = None,
    preview_id: str | None = None,
    generation: str | None = None,
    count: int | None = None,
    receipt_ids: Sequence[str] = (),
    has_more: bool | None = None,
) -> dict[str, object]:
    return {
        "kind": "progress",
        "phase": phase,
        "source_id": source_id,
        "preview_id": preview_id,
        "generation": generation,
        "count": count,
        "policy_sha256": None,
        "receipt_ids": list(receipt_ids),
        "selection_id": None,
        "approved_copy_ids": [],
        "has_more": has_more,
    }


class _Projected:
    def __init__(self, summary: Mapping[str, object], text: str) -> None:
        self.summary = summary
        self.text = text


class GuideFlow:
    """Coordinator for one guide invocation against one explicit set of paths."""

    def __init__(self, paths: GuidePaths, *, environment: Mapping[str, str] | None = None) -> None:
        self.paths = paths
        self.journal = WorkflowJournal(paths.journal)
        self.environment = dict(os.environ if environment is None else environment)
        self._executable: CollectorExecutable | None = None

    # -- entry -------------------------------------------------------------

    def advance(self, decision: Decision) -> dict[str, object]:
        try:
            return self._advance(decision)
        except _Stop as stop:
            return result_envelope(
                decision.action,
                stop.outcome,
                stop.code,
                document=stop.document,
                workflow_id=decision.workflow_id if stop.document is None else None,
                data=stop.data,
                next_actions=stop.next_actions,
            )
        except (GuideAdapterError, JournalError) as error:
            return result_envelope(
                decision.action, "refused", error.code, workflow_id=decision.workflow_id
            )

    def _advance(self, decision: Decision) -> dict[str, object]:
        executable = resolve_collector_executable(self.paths.executable)
        self._executable = executable
        if decision.action == "start":
            return self._start(decision, executable)
        document = self.journal.read()
        if document is None:
            raise _Stop("state_uninitialized")
        self.bind(document, executable)
        if decision.workflow_id != document["workflow_id"]:
            raise _Stop("binding_mismatch", document=document)
        if document["status"] != "active":
            raise _Stop("invalid_request", document=document)
        if decision.expected_revision != document["revision"]:
            raise _Stop("revision_conflict", document=document)
        document = self._settle(document, decision.action)
        if decision.action != "cancel":
            collector_handshake(executable)
        unresolved = self._unresolved(document)
        if unresolved and decision.action not in {"reconcile", "cancel"}:
            raise _Stop(
                "operation_unknown", document=document, next_actions=("reconcile", "cancel")
            )
        handler = getattr(self, "_" + decision.action.replace("-", "_"))
        return cast(dict[str, object], handler(document, decision))

    def bind(self, document: Mapping[str, object], executable: CollectorExecutable) -> None:
        binding = cast(dict[str, object], document["binding"])
        if (
            binding["brain_root"] != os.fspath(self.paths.brain)
            or binding["collector_state"] != os.fspath(self.paths.state)
            or binding["collector_binding"] != _collector_binding(self.paths)
        ):
            raise _Stop("binding_mismatch", document=document)
        if binding["executable"] != _executable_binding(executable):
            raise _Stop("collector_changed", document=document)

    # -- journal state -----------------------------------------------------

    def _write(
        self, document: Mapping[str, object], *, reserve: int = 16_384
    ) -> dict[str, object]:
        return self.journal.write(
            document, expected_revision=cast(int, document["revision"]), reserve_bytes=reserve
        )

    def _settle(self, document: dict[str, object], action: str) -> dict[str, object]:
        """An executing marker whose guard is free became unknown; a held guard is busy."""
        operations = cast(list[dict[str, object]], document["operations"])
        executing = [item for item in operations if item["state"] == "executing"]
        if not executing:
            return document
        workflow_id = cast(str, document["workflow_id"])
        if not self.journal.guard_free(workflow_id):
            raise _Stop("journal_busy", document=document)
        updated = copy.deepcopy(document)
        for item in cast(list[dict[str, object]], updated["operations"]):
            if item["state"] == "executing":
                item.update(state="unknown", executing_pid=None)
        updated = self._write(updated)
        if action not in {"reconcile", "cancel"}:
            raise _Stop(
                "operation_unknown", document=updated, next_actions=("reconcile", "cancel")
            )
        return updated

    @staticmethod
    def _unresolved(document: Mapping[str, object]) -> list[int]:
        operations = cast(list[dict[str, object]], document["operations"])
        resolved: set[int] = set()
        for item in operations:
            if item["action"] == "reconcile" and item["state"] in {"completed", "refused"}:
                request = json.loads(cast(str, item["canonical_request_utf8"]))
                resolved.add(cast(int, request["operation_sequence"]))
        return [
            cast(int, item["sequence"])
            for item in operations
            if item["action"] != "reconcile"
            and item["state"] == "unknown"
            and item["sequence"] not in resolved
        ]

    def _record(
        self,
        document: Mapping[str, object],
        action: str,
        request: Mapping[str, object],
        *,
        evidence: Mapping[str, object] | None = None,
        choices: Mapping[str, object] | None = None,
        status: str | None = None,
        state: str = "completed",
    ) -> dict[str, object]:
        """Record one journal-only action in a single CAS write."""
        updated = copy.deepcopy(dict(document))
        operations = cast(list[dict[str, object]], updated["operations"])
        operation = operation_record(
            sequence=len(operations),
            action=action,
            request=request,
            expected_versions=(),
            execution_binding=self._execution_binding(updated, None),
        )
        operation.update(state=state, evidence=None if evidence is None else dict(evidence))
        operations.append(operation)
        if choices is not None:
            cast(dict[str, object], updated["choices"]).update(choices)
        if status is not None:
            updated["status"] = status
        return self._write(updated)

    def _execution_binding(
        self, document: Mapping[str, object], report: Mapping[str, object] | None
    ) -> dict[str, object]:
        binding = cast(dict[str, object], document["binding"])
        source = cast(dict[str, object], report["source"]) if report is not None else {}
        return {
            "brain_id": binding["brain_id"],
            "issuer_epoch": binding["issuer_epoch"],
            "collector_binding": binding["collector_binding"],
            "source_generation": source.get("generation"),
            "control_epoch": source.get("control_epoch"),
            "executable": binding["executable"],
        }

    # -- intents and guarded execution --------------------------------------

    @staticmethod
    def _intent(operation: Mapping[str, object], summary: str) -> dict[str, object]:
        preimage = json.loads(cast(str, operation["canonical_intent_utf8"]))
        return {
            "action": operation["action"],
            "intent_sha256": operation["intent_sha256"],
            "request_sha256": operation["request_sha256"],
            "summary": summary[:1024],
            "canonical_request_utf8": operation["canonical_request_utf8"],
            "expected_versions": preimage["expected_versions"],
            "execution_binding": preimage["execution_binding"],
        }

    def _propose(
        self,
        document: dict[str, object],
        decision: Decision,
        *,
        request: Mapping[str, object],
        versions: Sequence[Mapping[str, object]],
        report: Mapping[str, object] | None,
        summary: str,
    ) -> tuple[dict[str, object], int | dict[str, object]]:
        """Return (document, prepared index) when confirmed, else a needs_confirmation result."""
        brain_id, issuer_epoch = brain_identity(self.paths.brain)
        binding = cast(dict[str, object], document["binding"])
        if (brain_id, issuer_epoch) != (binding["brain_id"], binding["issuer_epoch"]):
            raise _Stop("binding_mismatch", document=document)
        operations = cast(list[dict[str, object]], document["operations"])
        fresh = operation_record(
            sequence=len(operations),
            action=decision.action,
            request=request,
            expected_versions=versions,
            execution_binding=self._execution_binding(document, report),
        )
        if decision.confirmation is None:
            updated = copy.deepcopy(document)
            cast(list[dict[str, object]], updated["operations"]).append(fresh)
            # The exact intent preimage is persisted before it is shown.
            updated = self._write(updated)
            return updated, result_envelope(
                decision.action,
                "needs_confirmation",
                document=updated,
                intent=self._intent(fresh, summary),
                next_actions=(decision.action,),
            )
        wanted = decision.confirmation["intent_sha256"]
        matches = [
            index
            for index, item in enumerate(operations)
            if item["action"] == decision.action
            and item["state"] == "prepared"
            and item["intent_sha256"] == wanted
        ]
        if not matches:
            raise _Stop("revision_conflict", document=document)
        index = matches[-1]
        if wanted != fresh["intent_sha256"]:
            # Changed content, versions or executable invalidate the confirmation.
            updated = copy.deepcopy(document)
            cast(list[dict[str, object]], updated["operations"])[index]["state"] = "refused"
            raise _Stop("revision_conflict", document=self._write(updated))
        return document, index

    def _execute(
        self,
        document: dict[str, object],
        index: int,
        call: Callable[[int], dict[str, object]],
        complete: Callable[[dict[str, object], dict[str, object]], None],
    ) -> tuple[dict[str, object], dict[str, object] | None, GuideAdapterError | None]:
        """Guard, mark executing, run one child, then write its result at the reserved revision."""
        workflow_id = cast(str, document["workflow_id"])
        guard = self.journal.acquire_guard(workflow_id)
        try:
            updated = copy.deepcopy(document)
            operation = cast(list[dict[str, object]], updated["operations"])[index]
            operation.update(confirmation="confirmed", state="executing", executing_pid=os.getpid())
            updated = self._write(updated, reserve=_EXECUTION_RESERVE)
            try:
                value: dict[str, object] | None = call(guard)
                failure: GuideAdapterError | None = None
            except GuideAdapterError as error:
                value, failure = None, error
            finished = copy.deepcopy(updated)
            operation = cast(list[dict[str, object]], finished["operations"])[index]
            operation["executing_pid"] = None
            if failure is None:
                operation["state"] = "completed"
                complete(finished, cast(dict[str, object], value))
            else:
                operation["state"] = (
                    "unknown" if failure.code == "operation_unknown" else "refused"
                )
            try:
                finished = self.journal.write(
                    finished, expected_revision=cast(int, updated["revision"]), reserve_bytes=0
                )
            except JournalError:
                raise _Stop(
                    "operation_unknown", document=updated, next_actions=("reconcile",)
                ) from None
            return finished, value, failure
        finally:
            os.close(guard)

    def _metadata(self, *, source_id: str) -> dict[str, object]:
        assert self._executable is not None
        return read_guide_metadata(
            self._executable, state=self.paths.state, brain=self.paths.brain, source_id=source_id
        )

    @contextmanager
    def _guarded(self, document: Mapping[str, object]) -> Iterator[int]:
        fd = self.journal.acquire_guard(cast(str, document["workflow_id"]))
        try:
            yield fd
        finally:
            os.close(fd)

    # -- actions -----------------------------------------------------------

    def _start(self, decision: Decision, executable: CollectorExecutable) -> dict[str, object]:
        if decision.expected_revision != 0:
            raise _Stop("revision_conflict")
        existing = self.journal.read()
        if existing is not None:
            # Lost create: the exact matching journal is read back, never replaced.
            self.bind(existing, executable)
            if existing["workflow_id"] != decision.workflow_id:
                raise _Stop("binding_mismatch", document=existing)
            return result_envelope("start", "completed", document=existing,
                                   next_actions=("preflight",))
        collector_handshake(executable)
        brain_id, issuer_epoch = brain_identity(self.paths.brain)
        document = new_journal(
            decision.workflow_id,
            {
                "brain_id": brain_id,
                "issuer_epoch": issuer_epoch,
                "brain_root": os.fspath(self.paths.brain),
                "collector_state": os.fspath(self.paths.state),
                "collector_binding": _collector_binding(self.paths),
                "executable": _executable_binding(executable),
            },
        )
        self.journal.create(document)
        return result_envelope("start", "completed", document=document,
                               next_actions=("preflight",))

    def _preflight(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        from open_brain.local_data import select_local_root
        from open_brain.services.agent_settings_observation import (
            SettingsObservationError,
            observe_client_settings,
        )

        values = decision.arguments
        context = Path(cast(str, values["context_path"]))
        for key in ("context_path", "consent_state", "policy_path", "runtime_path"):
            candidate = Path(cast(str, values[key]))
            for store in (self.paths.brain, self.paths.state.parent / "live",
                          self.paths.journal.parent):
                if candidate == store or store in candidate.parents:
                    raise _Stop("invalid_request", document=document)
        try:
            selection = select_local_root(
                data_dir=os.fspath(self.paths.brain), environment=self.environment
            )
            observation = observe_client_settings(
                client=cast(str, values["client_kind"]),
                client_version=values["client_version"],
                project=context if values["scope"] == "project" else None,
                home=selection.home,
                environment=self.environment,
                launch_context=values["launch_context"],
                platform_name=selection.platform_name,
                runtime=Path(cast(str, values["runtime_path"])),
            )
        except (SettingsObservationError, OSError, ValueError):
            raise _Stop("unscoped_client", document=document) from None
        if observation.public["unscoped_entries"]:
            raise _Stop("unscoped_client", document=document)
        updated = self._record(document, "preflight", values, choices=values)
        return result_envelope(
            "preflight",
            "completed",
            document=updated,
            data=_progress("preflight"),
            next_actions=("accounts",),
        )

    def _accounts(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        assert self._executable is not None
        with self._guarded(document) as guard:
            accounts = list_accounts(
                self._executable, state=self.paths.state, brain=self.paths.brain, guard_fd=guard
            )
        updated = self._record(document, "accounts", {})
        return result_envelope(
            "accounts", "completed", document=updated, data=accounts, next_actions=("resources",)
        )

    def _inventory(
        self, document: Mapping[str, object], connection_id: str, cursor: str | None
    ) -> dict[str, object]:
        assert self._executable is not None
        with self._guarded(document) as guard:
            return read_resource_inventory(
                self._executable,
                state=self.paths.state,
                brain=self.paths.brain,
                guard_fd=guard,
                connection_id=connection_id,
                cursor=cursor,
            )

    def _resources(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        connection = cast(str, decision.arguments["connection_id"])
        cursor = cast(str | None, decision.arguments["cursor"])
        inventory = self._inventory(document, connection, cursor)
        updated = self._record(
            document, "resources", {"connection_id": connection, "cursor": cursor}
        )
        more = inventory["has_more"] is True
        return result_envelope(
            "resources",
            "partial" if more else "completed",
            document=updated,
            data=inventory,
            next_actions=("resources", "choose") if more else ("choose",),
        )

    def _choose(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        if configured_source(document) is not None:
            # A configured workflow keeps its source; a new channel is a new workflow.
            raise _Stop("invalid_request", document=document)
        values = decision.arguments
        connection = cast(str, values["connection_id"])
        assert self._executable is not None
        with self._guarded(document) as guard:
            accounts = list_accounts(
                self._executable, state=self.paths.state, brain=self.paths.brain, guard_fd=guard
            )
        rows = cast(list[dict[str, object]], accounts["rows"])
        if not any(row["connection_id"] == connection for row in rows):
            raise _Stop("invalid_request", document=document)
        # Re-read from the page the owner last listed: only an actual returned row is chosen.
        cursor: str | None = None
        for item in reversed(cast(list[dict[str, object]], document["operations"])):
            if item["action"] == "resources" and item["state"] == "completed":
                listed = json.loads(cast(str, item["canonical_request_utf8"]))
                if listed["connection_id"] == connection:
                    cursor = listed["cursor"]
                    break
        inventory = self._inventory(document, connection, cursor)
        resources = cast(list[dict[str, object]], inventory["rows"])
        if not any(
            row["resource_id"] == values["resource_id"]
            and row["resource_type"] == values["resource_type"]
            for row in resources
        ):
            raise _Stop("invalid_request", document=document)
        updated = self._record(document, "choose", values, choices=values)
        return result_envelope(
            "choose",
            "completed",
            document=updated,
            data=_progress("choose"),
            next_actions=("configure",),
        )

    def _selection_report(self, document: Mapping[str, object]) -> dict[str, object]:
        """Read HealthV1 by the exact chosen selection through a private temporary file."""
        assert self._executable is not None
        choices = cast(dict[str, object], document["choices"])
        selection = {
            "connector_name": "slack",
            "connection_id": choices["connection_id"],
            "resource_id": choices["resource_id"],
            "resource_type": choices["resource_type"],
        }
        path = self.paths.journal.with_name(
            f".{self.paths.journal.name}.selection-{os.getpid()}.json"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                os.write(fd, canonical_json(selection).encode("utf-8"))
            finally:
                os.close(fd)
            return read_guide_metadata(
                self._executable, state=self.paths.state, brain=self.paths.brain,
                selection_file=path,
            )
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _require_snapshot(document: Mapping[str, object], report: Mapping[str, object]) -> None:
        snapshot = cast(dict[str, object], report["snapshot"])
        reasons = cast(list[str], snapshot["reason_codes"])
        if snapshot["consistency"] == "inconsistent":
            raise _Stop("snapshot_inconsistent", document=document)
        if snapshot["consistency"] == "unavailable":
            raise _Stop(
                "binding_mismatch" if "binding_mismatch" in reasons else "snapshot_inconsistent",
                document=document,
            )

    @staticmethod
    def _source_versions(
        source_id: str, report: Mapping[str, object]
    ) -> list[dict[str, object]]:
        source = cast(dict[str, object], report["source"])
        generation, epoch = source["generation"], source["control_epoch"]
        return [
            {
                "kind": "configuration",
                "id": source_id,
                "version": "absent" if generation is None else cast(str, generation),
            },
            {
                "kind": "collector",
                "id": source_id,
                "version": "absent" if epoch is None else str(epoch),
            },
        ]

    def _configure(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        operations = cast(list[dict[str, object]], document["operations"])
        completed = {item["action"] for item in operations if item["state"] == "completed"}
        if not {"preflight", "choose"} <= completed:
            raise _Stop("inspection_required", document=document,
                        next_actions=("preflight", "choose"))
        if configured_source(document) is not None:
            raise _Stop("invalid_request", document=document)
        report = self._selection_report(document)
        self._require_snapshot(document, report)
        source = cast(dict[str, object], report["source"])
        source_id = cast(str, source["source_id"])
        if source["configured_status"] in {"enabled", "paused"}:
            # An active source is never adopted by the guide.
            raise _Stop("authority_refused", document=document)
        choices = cast(dict[str, object], document["choices"])
        request = {
            "selection": {
                "connector_name": "slack",
                "connection_id": choices["connection_id"],
                "resource_id": choices["resource_id"],
                "resource_type": choices["resource_type"],
            },
            "options": {"date_floor": choices["date_floor"], "privacy_tier": "work"},
            "source_id": source_id,
        }
        summary = (
            f"Configure Slack channel {choices['resource_id']} from {choices['date_floor']} "
            "as Work. Collection stays manual; no schedule or service is enabled."
        )
        document, prepared = self._propose(
            document,
            decision,
            request=request,
            versions=self._source_versions(source_id, report),
            report=report,
            summary=summary,
        )
        if isinstance(prepared, dict):
            return prepared
        saved = cast(list[dict[str, object]], document["operations"])[prepared]
        saved_request = cast(str, saved["canonical_request_utf8"])
        arguments = self.paths.journal.with_name(
            f".{self.paths.journal.name}.configure-{prepared}.json"
        )

        def call(guard: int) -> dict[str, object]:
            assert self._executable is not None
            fd = os.open(arguments, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    # Exactly the saved request bytes, never a reconstruction.
                    os.write(fd, saved_request.encode("utf-8"))
                finally:
                    os.close(fd)
                value = configure_source(
                    self._executable,
                    state=self.paths.state,
                    brain=self.paths.brain,
                    guard_fd=guard,
                    arguments_file=arguments,
                )
            finally:
                arguments.unlink(missing_ok=True)
            if value["source_id"] != source_id:
                raise GuideAdapterError("operation_unknown")
            return value

        def complete(finished: dict[str, object], _value: dict[str, object]) -> None:
            operation = cast(list[dict[str, object]], finished["operations"])[prepared]
            operation["evidence"] = {"source_id": source_id, "preview_id": None, "receipt_ids": []}

        finished, _value, failure = self._execute(document, prepared, call, complete)
        if failure is not None:
            self._failed(finished, "configure", failure)
        return result_envelope(
            "configure",
            "completed",
            document=finished,
            data=_progress("configure", source_id=source_id),
            next_actions=("preview",),
        )

    @staticmethod
    def _failed(
        document: Mapping[str, object], action: str, failure: GuideAdapterError
    ) -> None:
        if failure.code == "operation_unknown":
            raise _Stop("operation_unknown", document=document, next_actions=("reconcile",))
        raise _Stop(failure.code, document=document)

    def _selected(self, document: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        source_id = configured_source(document)
        if source_id is None:
            raise _Stop("inspection_required", document=document, next_actions=("configure",))
        report = self._metadata(source_id=source_id)
        self._require_snapshot(document, report)
        source = cast(dict[str, object], report["source"])
        if source["generation"] is None:
            raise _Stop("source_reset_required", document=document)
        if source["configured_status"] != "disabled":
            raise _Stop("authority_refused", document=document)
        return source_id, report

    def _preview(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        source_id, report = self._selected(document)
        pending = cast(dict[str, object], report["pending_preview"])
        generation = cast(str, cast(dict[str, object], report["source"])["generation"])
        if pending["status"] == "present":
            # A retained page is imported or inspected; preview never rediscovers it.
            raise _Stop(
                "inspection_required",
                document=document,
                data=_progress(
                    "preview",
                    source_id=source_id,
                    preview_id=cast(str, pending["preview_id"]),
                    generation=generation,
                    count=cast(int | None, pending["count"]),
                    has_more=cast(bool | None, pending["has_more"]),
                ),
                next_actions=("import",),
            )
        if pending["status"] != "absent":
            raise _Stop("snapshot_inconsistent", document=document)
        document, prepared = self._propose(
            document,
            decision,
            request={"source_id": source_id},
            versions=self._source_versions(source_id, report),
            report=report,
            summary=(
                "Fetch one Slack page for the selected Work channel over the network and keep it "
                "as a local pending preview. Nothing is imported yet."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def call(guard: int) -> dict[str, object]:
            assert self._executable is not None
            return preview_source(
                self._executable,
                state=self.paths.state,
                brain=self.paths.brain,
                guard_fd=guard,
                source_id=source_id,
            )

        def complete(finished: dict[str, object], value: dict[str, object]) -> None:
            operation = cast(list[dict[str, object]], finished["operations"])[prepared]
            operation["evidence"] = {
                "source_id": source_id,
                "preview_id": value["preview_id"],
                "receipt_ids": [],
            }

        finished, value, failure = self._execute(document, prepared, call, complete)
        if failure is not None:
            self._failed(finished, "preview", failure)
        result = cast(dict[str, object], value)
        return result_envelope(
            "preview",
            "completed",
            document=finished,
            data=_progress(
                "preview",
                source_id=source_id,
                preview_id=cast(str, result["preview_id"]),
                generation=generation,
                count=cast(int, result["count"]),
                has_more=cast(bool, result["has_more"]),
            ),
            next_actions=("import",),
        )

    def _import_call(
        self, source_id: str, preview_id: str
    ) -> Callable[[int], dict[str, object]]:
        def call(guard: int) -> dict[str, object]:
            assert self._executable is not None
            return import_preview(
                self._executable,
                state=self.paths.state,
                brain=self.paths.brain,
                guard_fd=guard,
                source_id=source_id,
                preview_id=preview_id,
            )

        return call

    @staticmethod
    def _retain(finished: dict[str, object], receipts: Sequence[str]) -> None:
        retained = cast(list[str], finished["retained_receipt_ids"])
        retained.extend(item for item in receipts if item not in retained)

    @staticmethod
    def _imported(
        action: str, finished: Mapping[str, object], value: Mapping[str, object]
    ) -> dict[str, object]:
        more = value["has_more"] is True
        return result_envelope(
            action,
            "completed",
            document=finished,
            data=_progress(
                "import",
                source_id=cast(str, value["source_id"]),
                preview_id=cast(str, value["preview_id"]),
                count=cast(int, value["captured_count"]),
                receipt_ids=cast(list[str], value["receipt_ids"]),
                has_more=more,
            ),
            next_actions=("inspect", "preview") if more else ("inspect",),
        )

    def _import(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        preview_id = cast(str, decision.arguments["preview_id"])
        source_id, report = self._selected(document)
        pending = cast(dict[str, object], report["pending_preview"])
        if pending["status"] != "present" or pending["preview_id"] != preview_id:
            # Expired, replaced or unknown pages never import; a new preview is a new intent.
            raise _Stop("authority_refused", document=document, next_actions=("preview",))
        if len(cast(list[str], document["retained_receipt_ids"])) + _PAGE_RECEIPTS > _MAX_RETAINED:
            raise _Stop("capacity_reached", document=document)
        versions: list[dict[str, object]] = [
            *self._source_versions(source_id, report),
            {"kind": "collector", "id": preview_id, "version": "pending"},
        ]
        document, prepared = self._propose(
            document,
            decision,
            request={"preview_id": preview_id, "source_id": source_id},
            versions=versions,
            report=report,
            summary=(
                f"Import exactly the pending page {preview_id} ({pending['count']} records) as "
                "local Work captures. The next page is not fetched."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: dict[str, object]) -> None:
            operation = cast(list[dict[str, object]], finished["operations"])[prepared]
            receipts = cast(list[str], value["receipt_ids"])
            operation["evidence"] = {
                "source_id": source_id,
                "preview_id": preview_id,
                "receipt_ids": list(receipts),
            }
            self._retain(finished, receipts)

        finished, value, failure = self._execute(
            document, prepared, self._import_call(source_id, preview_id), complete
        )
        if failure is not None:
            self._failed(finished, "import", failure)
        return self._imported("import", finished, cast(dict[str, object], value))

    def _read_inspection(
        self, document: Mapping[str, object], receipt_id: str
    ) -> dict[str, object]:
        source_id = configured_source(document)
        if source_id is None or receipt_id not in cast(
            list[str], document["retained_receipt_ids"]
        ):
            raise _Stop("receipt_unresolved", document=document)
        binding = cast(dict[str, object], document["binding"])
        if brain_identity(self.paths.brain) != (binding["brain_id"], binding["issuer_epoch"]):
            raise _Stop("binding_mismatch", document=document)
        report = self._metadata(source_id=source_id)
        try:
            record = resolve_captured_record(
                receipt_id=receipt_id,
                retained_receipt_ids=cast(list[str], document["retained_receipt_ids"]),
                selected_source_id=source_id,
                bindings=cast(list[dict[str, object]], report["receipt_bindings"]),
                read_record=self._read_record,
            )
        except GuideAdapterError as error:
            raise _Stop(error.code, document=document) from None
        body_bytes = record.body.encode("utf-8")
        if len(body_bytes) > _MAX_BODY_BYTES:
            raise _Stop("capacity_reached", document=document)
        return {
            "kind": "inspection",
            "receipt_id": record.receipt_id,
            "capture_id": record.capture_id,
            "source_id": record.source_id,
            "revision_id": record.revision_id,
            "projection_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "body": record.body,
            "current": record.current,
        }

    def _read_record(self, capture_id: str) -> _Projected:
        """Owner complete read of the literal capture at that same expected revision."""
        from open_brain.local_data import select_local_root
        from open_brain.services.local_bootstrap import open_local_brain
        from open_brain.services.t03_adapters import T03AppAdapter, owner_authority

        selection = select_local_root(
            data_dir=os.fspath(self.paths.brain), environment=self.environment
        )
        with open_local_brain(selection, existing_only=True) as session:
            adapter = T03AppAdapter(
                session.tasks, owner_authority(session.tasks, session_id="owner-guide")
            )
            text, cursor, summary = "", None, None
            for _chunk in range(_MAX_READ_CHUNKS):
                response = adapter.invoke(
                    "record.read",
                    {
                        "dto_version": 1,
                        "record_id": capture_id,
                        "expected_revision_id": capture_id,
                        "target_bytes": 32_768,
                        "cursor": cursor,
                    },
                )
                record = cast(dict[str, object], response["record"])
                if summary is not None and record != summary:
                    raise ValueError("record changed during read")
                summary = record
                text += cast(str, cast(dict[str, object], response["content"])["text"])
                cursor = cast(str | None, response["next_cursor"])
                if cursor is None:
                    break
            else:
                raise ValueError("record exceeds inspection capacity")
        if summary is None:
            raise ValueError("record unavailable")
        return _Projected(summary, text)

    def _inspect(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        receipt_id = cast(str, decision.arguments["receipt_id"])
        inspection = self._read_inspection(document, receipt_id)
        updated = self._record(
            document,
            "inspect",
            {"receipt_id": receipt_id},
            evidence={
                "projection_sha256": inspection["projection_sha256"],
                "revision_id": inspection["revision_id"],
            },
        )
        return result_envelope(
            "inspect",
            "completed",
            document=updated,
            data=inspection,
            next_actions=("acknowledge-inspection",),
        )

    def _acknowledge_inspection(
        self, document: dict[str, object], decision: Decision
    ) -> dict[str, object]:
        receipt_id = cast(str, decision.arguments["receipt_id"])
        digest = cast(str, decision.arguments["inspection_sha256"])
        inspected = [
            cast(dict[str, object], item["evidence"])
            for item in cast(list[dict[str, object]], document["operations"])
            if item["action"] == "inspect"
            and item["state"] == "completed"
            and json.loads(cast(str, item["canonical_request_utf8"]))["receipt_id"] == receipt_id
        ]
        if not inspected or inspected[-1]["projection_sha256"] != digest:
            raise _Stop("inspection_required", document=document, next_actions=("inspect",))
        # Re-read now: an acknowledgement binds only the current displayed projection.
        current = self._read_inspection(document, receipt_id)
        if current["projection_sha256"] != digest or not current["current"]:
            raise _Stop("inspection_required", document=document, next_actions=("inspect",))
        document, prepared = self._propose(
            document,
            decision,
            request={"inspection_sha256": digest, "receipt_id": receipt_id},
            versions=[
                {
                    "kind": "source",
                    "id": cast(str, current["source_id"]),
                    "version": cast(str, current["revision_id"]),
                }
            ],
            report=None,
            summary=(
                "Record that you read the complete displayed message. This is not release "
                "approval and does not route or share it."
            ),
        )
        if isinstance(prepared, dict):
            return prepared
        updated = copy.deepcopy(document)
        operation = cast(list[dict[str, object]], updated["operations"])[prepared]
        operation.update(
            confirmation="confirmed",
            state="completed",
            evidence={"projection_sha256": digest, "revision_id": current["revision_id"]},
        )
        updated = self._write(updated)
        return result_envelope(
            "acknowledge-inspection",
            "completed",
            document=updated,
            data=_progress("acknowledge-inspection", source_id=cast(str, current["source_id"])),
        )

    def _reconcile(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        sequence = cast(int, decision.arguments["operation_sequence"])
        operations = cast(list[dict[str, object]], document["operations"])
        if sequence >= len(operations) or sequence not in self._unresolved(document):
            raise _Stop("invalid_request", document=document)
        target = operations[sequence]
        request = {"operation_sequence": sequence, "request_sha256": target["request_sha256"]}
        action = cast(str, target["action"])
        if action != "import":
            # Halt-only: no authority call, no new operation ID, no compensating write.
            updated = self._record(document, "reconcile", request, state="refused")
            data = None
            source_id = configured_source(updated)
            if action == "preview" and source_id is not None:
                pending = cast(
                    dict[str, object], self._metadata(source_id=source_id)["pending_preview"]
                )
                if pending["status"] == "present":
                    data = _progress(
                        "preview",
                        source_id=source_id,
                        preview_id=cast(str, pending["preview_id"]),
                        count=cast(int | None, pending["count"]),
                        has_more=cast(bool | None, pending["has_more"]),
                    )
            raise _Stop(
                "operation_unknown",
                document=updated,
                data=data,
                next_actions=("import", "preview") if data is not None else (action,),
            )
        saved = json.loads(cast(str, target["canonical_request_utf8"]))
        source_id, preview_id = cast(str, saved["source_id"]), cast(str, saved["preview_id"])
        document, prepared = self._propose(
            document,
            decision,
            request=request,
            versions=[{"kind": "collector", "id": preview_id, "version": "replay"}],
            report=None,
            summary=(
                f"Replay the exact saved import request for {preview_id}. The collector returns "
                "the completed receipt or refuses; nothing new is fetched."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: dict[str, object]) -> None:
            original = cast(list[dict[str, object]], finished["operations"])[sequence]
            receipts = cast(list[str], value["receipt_ids"])
            original.update(
                state="completed",
                evidence={
                    "source_id": source_id,
                    "preview_id": preview_id,
                    "receipt_ids": list(receipts),
                },
            )
            self._retain(finished, receipts)

        finished, value, failure = self._execute(
            document, prepared, self._import_call(source_id, preview_id), complete
        )
        if failure is not None:
            if failure.code == "operation_unknown":
                raise _Stop("operation_unknown", document=finished, next_actions=("reconcile",))
            # A refused replay halts: the exact saved request is never altered.
            raise _Stop(failure.code, document=finished, next_actions=("cancel",))
        return self._imported("reconcile", finished, cast(dict[str, object], value))

    def _cancel(self, document: dict[str, object], _decision: Decision) -> dict[str, object]:
        updated = self._record(document, "cancel", {}, status="cancelled")
        return result_envelope("cancel", "cancelled", document=updated)

    # -- status ------------------------------------------------------------

    def selected_source(self, executable: CollectorExecutable) -> tuple[dict[str, object] | None,
                                                                          str | None]:
        """Read-only: the journal (if any) and its selected source, never writing."""
        document = self.journal.read()
        if document is None:
            return None, None
        self.bind(document, executable)
        return document, configured_source(document)


def stop_envelope(action: str, error: Exception) -> dict[str, object] | None:
    """Map a coordinator refusal to an envelope for callers outside `advance`."""
    if isinstance(error, _Stop):
        return result_envelope(
            action,
            error.outcome,
            error.code,
            document=error.document,
            data=error.data,
            next_actions=error.next_actions,
        )
    return None


def is_stop(error: BaseException) -> bool:
    return isinstance(error, _Stop)
