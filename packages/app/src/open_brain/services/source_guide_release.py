"""Guide owner release and scoped setup (P3): one named decision, one owner write.

`ReleaseFlow` extends the capture coordinator with the remaining RECOVERY.md
transitions: destination, create-destination, route, consent, select,
release-preview, approve, policy-preview, policy-apply, setup-preview and
setup-apply. Every write persists its exact canonical request before the
existing owner authority runs it and needs its own owner confirmation. A lost
acknowledgement stops advancement: create-destination, route, consent, select,
release-preview and approve replay their exact saved request through the same
authority on an explicit `reconcile`; policy-apply, setup-preview and
setup-apply halt and require a fresh preview. A refused replay never proves
that the earlier attempt did not commit, so it halts the workflow. Nothing here
grants implicit consent or approval, approves in bulk, launches a client or
model, or writes policy/config bodies into the journal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from open_brain.services.source_guide_adapter import GuideAdapterError
from open_brain.services.source_guide_flow import (
    Decision,
    GuideFlow,
    _progress,
    _Stop,
    brain_identity,
    result_envelope,
)
from open_brain.services.source_guide_journal import canonical_json, sha256_text

__all__ = ["RECALL_TASK", "ReleaseFlow"]

_REPLAYABLE: Final = frozenset(
    {"create-destination", "route", "consent", "select", "release-preview", "approve"}
)
# No usable all-files or create receipt: an unknown outcome halts and repreviews.
_REPREVIEW: Final = {
    "policy-apply": "policy-preview",
    "setup-preview": "setup-preview",
    "setup-apply": "setup-preview",
}
RECALL_TASK: Final = (
    "In the configured client, ask: search the Open Brain Work release space for the "
    "approved Slack messages, read each result, and answer only from them with record "
    "and revision citations. Slack coverage beyond the selected approved messages is unknown."
)


class _HaltingRefusal(GuideAdapterError):
    """A refusal after which the authority state cannot be proven unchanged."""


def _classify(error: Exception, *, action: str) -> GuideAdapterError:
    """Map an owner authority failure to a closed guide code.

    Only typed validation refusals that run before the authority commits are
    plain refusals. Anything else may have committed and is an unknown outcome.
    """
    from open_brain_engine.engine.consent_contracts import ConsentContractError
    from open_brain_engine.engine.sharing_contracts import SharingError
    from open_brain_engine.engine.t03_contracts import T03Error

    from open_brain.services.agent_setup import AgentSetupFailure
    from open_brain.services.scoped_policy_authoring import PolicyAuthoringError
    from open_brain.services.session_consent import SessionConsentError

    if isinstance(error, GuideAdapterError):
        return error
    if isinstance(error, T03Error):
        if error.code in {"revision_changed", "source_revision_conflict"}:
            return GuideAdapterError("revision_conflict")
        if error.code in {"not_found", "invalid_arguments", "unsupported_capability"}:
            return GuideAdapterError("authority_refused")
        return GuideAdapterError("operation_unknown")
    if isinstance(error, SharingError):
        if error.code in {"revision_changed", "reapproval_required", "binding_mismatch"}:
            return GuideAdapterError("revision_conflict")
        if error.code in {"invalid_arguments", "not_found", "preview_expired",
                          "unsupported_capability"}:
            return GuideAdapterError("authority_refused")
        return GuideAdapterError("operation_unknown")
    if isinstance(error, SessionConsentError):
        if error.code in {"consent_state_conflict", "consent_state_mismatch"}:
            # File/root CAS or foreign state: halt, never retry automatically.
            return _HaltingRefusal("authority_refused")
        # The store can fail after its atomic replace; a same-ID replay is idempotent.
        return GuideAdapterError("operation_unknown")
    if isinstance(error, ConsentContractError):
        return GuideAdapterError("authority_refused")
    if isinstance(error, PolicyAuthoringError):
        if action == "policy-apply" and error.code == "unsafe_path":
            # A create may have reached the file system before the failure.
            return GuideAdapterError("operation_unknown")
        return GuideAdapterError(error.code)
    if isinstance(error, AgentSetupFailure):
        if action == "setup-apply" and error.code in {"setup_conflict", "operation_failed"}:
            return GuideAdapterError("operation_unknown")
        return GuideAdapterError("authority_refused")
    if isinstance(error, ValueError) and action == "create-destination":
        # Space operation validation and delivery conflicts refuse before any write.
        return GuideAdapterError("authority_refused")
    return GuideAdapterError("operation_unknown")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(operation: Mapping[str, object]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(cast(str, operation["canonical_request_utf8"])))


class ReleaseFlow(GuideFlow):
    """Capture coordinator plus owner release, policy and scoped setup actions."""

    # -- shared helpers ------------------------------------------------------

    @staticmethod
    def _operations(document: Mapping[str, object]) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], document["operations"])

    def _completed(self, document: Mapping[str, object], action: str) -> list[dict[str, object]]:
        return [
            item
            for item in self._operations(document)
            if item["action"] == action and item["state"] == "completed"
        ]

    @staticmethod
    def _choices(document: Mapping[str, object]) -> dict[str, object]:
        return cast(dict[str, object], document["choices"])

    def _identity(self, document: Mapping[str, object]) -> tuple[str, int]:
        brain_id, issuer_epoch = brain_identity(self.paths.brain)
        binding = cast(dict[str, object], document["binding"])
        if (brain_id, issuer_epoch) != (binding["brain_id"], binding["issuer_epoch"]):
            raise _Stop("binding_mismatch", document=document)
        return brain_id, issuer_epoch

    @contextmanager
    def _owner(self) -> Iterator[tuple[Any, Any]]:
        """One existing-only owner session per authority step; never held across a prompt."""
        from open_brain.local_data import select_local_root
        from open_brain.services.local_bootstrap import open_local_brain
        from open_brain.services.t03_adapters import owner_authority

        selection = select_local_root(
            data_dir=str(self.paths.brain), environment=self.environment
        )
        with open_local_brain(selection, existing_only=True) as session:
            yield session.tasks, owner_authority(session.tasks, session_id="owner-guide")

    def _saved(
        self, document: Mapping[str, object], decision: Decision
    ) -> dict[str, object] | None:
        """The prepared request this confirmation names; its saved IDs are never regenerated."""
        if decision.confirmation is None:
            return None
        wanted = decision.confirmation["intent_sha256"]
        for item in reversed(self._operations(document)):
            if (
                item["action"] == decision.action
                and item["state"] == "prepared"
                and item["intent_sha256"] == wanted
            ):
                return _request(item)
        return None

    def _halt(self, document: dict[str, object]) -> dict[str, object]:
        halted = copy.deepcopy(document)
        halted["status"] = "halted"
        return self._write(halted, reserve=0)

    def _run(
        self,
        document: dict[str, object],
        index: int,
        action: str,
        request: Mapping[str, object],
        on_complete: Callable[[dict[str, object], object], None],
    ) -> tuple[dict[str, object], object]:
        """Execute one confirmed owner write under the lifetime guard with its saved request."""
        saved = _request(self._operations(document)[index])
        if saved != dict(request):
            raise _Stop("revision_conflict", document=document)

        def call(_guard: int) -> dict[str, object]:
            return {"value": self._authority(action, saved)}

        def complete(finished: dict[str, object], value: dict[str, object]) -> None:
            on_complete(finished, value["value"])

        finished, value, failure = self._execute(document, index, call, complete)
        if failure is not None:
            if isinstance(failure, _HaltingRefusal):
                finished = self._halt(finished)
            self._failed(finished, action, failure)
        return finished, cast(dict[str, object], value)["value"]

    def _authority(self, action: str, request: Mapping[str, object]) -> object:
        try:
            return self._call(action, request)
        except _Stop:
            raise
        except Exception as error:
            raise _classify(error, action=action) from None

    def _call(self, action: str, request: Mapping[str, object]) -> object:
        """Exactly one existing owner authority call built from the saved request bytes."""
        values: dict[str, Any] = dict(request)
        if action == "create-destination":
            with self._owner() as (tasks, _owner):
                return tasks.inbox.create_space(
                    cast(str, values["name"]), delivery_id=cast(str, values["delivery_id"])
                )
        if action == "route":
            from open_brain_engine.engine.t03_contracts import SourceRouteRequest

            with self._owner() as (tasks, owner):
                response = tasks.sources.route(SourceRouteRequest(**values), authority=owner)
                return response.to_wire()
        if action == "consent":
            from open_brain_engine.core.models import PrivacyTier

            store = self._consent_store(values)
            return store.grant(
                provider_id=cast(str, values["provider_id"]),
                allowed_tiers=frozenset(PrivacyTier(item) for item in
                                        cast(list[str], values["allowed_tiers"])),
                operation_id=cast(str, values["operation_id"]),
                decided_at=cast(str, values["decided_at"]),
            ).receipt
        if action == "select":
            from open_brain_engine.engine.sharing_contracts import SlackSelectionCreateRequest

            values["member_source_ids"] = tuple(cast(list[str], values["member_source_ids"]))
            with self._owner() as (tasks, owner):
                return tasks.sharing.create_selection(
                    SlackSelectionCreateRequest(**values), authority=owner
                )
        if action == "release-preview":
            from open_brain_engine.engine.sharing_contracts import SlackSharingPreviewRequest

            values["provider_ids"] = tuple(cast(list[str], values["provider_ids"]))
            with self._owner() as (tasks, owner):
                return tasks.sharing.preview(SlackSharingPreviewRequest(**values), authority=owner)
        if action == "approve":
            from open_brain_engine.engine.sharing_contracts import SharingDecisionRequest

            with self._owner() as (tasks, owner):
                return tasks.sharing.decide(SharingDecisionRequest(**values), authority=owner)
        if action == "policy-apply":
            from open_brain.services.scoped_policy_authoring import apply_scoped_policy

            return apply_scoped_policy(
                Path(cast(str, values["policy_path"])),
                self._policy_inputs(values),
                policy_sha256=cast(str, values["policy_sha256"]),
                expected_absent=cast(bool, values["expected_absent"]),
                consent_path=Path(cast(str, self._consent_path)),
            )
        if action == "setup-preview":
            return self._setup("preview", values)
        if action == "setup-apply":
            return self._setup("apply", values)
        raise GuideAdapterError("invalid_request")

    # -- destination ---------------------------------------------------------

    def _destination(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        # v1 refuses every existing space: a reused space can expose records outside this
        # selection, so the only destination is one this workflow creates.
        raise _Stop("authority_refused", document=document, next_actions=("create-destination",))

    def _workflow_captures(self, document: Mapping[str, object]) -> set[str]:
        """Captures this workflow placed or released: inspected/routed heads and its copies."""
        known: set[str] = set()
        for item in self._completed(document, "acknowledge-inspection"):
            known.add(cast(str, cast(dict[str, object], item["evidence"])["revision_id"]))
        for item in self._completed(document, "route"):
            known.add(cast(str, cast(dict[str, object], item["evidence"])["head"]))
        for item in self._completed(document, "release-preview"):
            known.add(cast(str, _request(item)["expected_head"]))
        for evidence in self._approved(document):
            if evidence["copy_capture_id"] is not None:
                known.add(cast(str, evidence["copy_capture_id"]))
        return known

    def _require_dedicated(self, document: Mapping[str, object], action: str) -> None:
        """Refuse (journal-visible, no authority write) if the space holds foreign records."""
        space_id = cast(str, self._choices(document)["destination_space_id"])
        try:
            with self._owner() as (tasks, _owner):
                captures, truncated = tasks.inbox.space_capture_ids(space_id, limit=1000)
        except Exception:
            captures, truncated = (), True
        if truncated or not set(captures) <= self._workflow_captures(document):
            # Another approval or capture in this space would be readable by the client.
            updated = self._record(
                document, action, {"destination_space_id": space_id}, state="refused"
            )
            raise _Stop("authority_refused", document=updated)

    def _create_destination(
        self, document: dict[str, object], decision: Decision
    ) -> dict[str, object]:
        if self._choices(document)["destination_space_id"] is not None:
            raise _Stop("invalid_request", document=document)
        brain_id, _epoch = self._identity(document)
        name = cast(str, decision.arguments["name"])
        saved = self._saved(document, decision)
        request = {
            "brain_id": brain_id,
            "delivery_id": (
                cast(str, saved["delivery_id"]) if saved is not None
                else "guide.space." + uuid.uuid4().hex
            ),
            "name": name,
        }
        document, prepared = self._propose(
            document, decision, request=request, versions=(), report=None,
            summary=(
                f"Create a new dedicated Work release space named {name!r}. Only messages "
                "you route and approve later are placed in it."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._space_completed(finished, prepared, request, value)

        finished, _value = self._run(document, prepared, "create-destination", request, complete)
        return result_envelope(
            "create-destination", "completed", document=finished,
            data=_progress("create-destination"), next_actions=("route",),
        )

    def _space_completed(
        self, finished: dict[str, object], index: int, request: Mapping[str, object],
        value: object,
    ) -> None:
        space_id = cast(str, cast(Any, value).space_id)
        operation = self._operations(finished)[index]
        operation["evidence"] = {"delivery_id": request["delivery_id"], "space_id": space_id}
        self._choices(finished)["destination_space_id"] = space_id

    # -- current members -----------------------------------------------------

    def _inspect_source(self, source_id: str) -> Any:
        from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest

        with self._owner() as (tasks, owner):
            return tasks.sources.inspect(SourceInspectRequest(source_id=source_id), authority=owner)

    def _member(
        self,
        document: Mapping[str, object],
        receipt_id: str,
        report: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """The acknowledged, still-current head for one retained receipt, or a refusal."""
        inspection = self._read_inspection(document, receipt_id, report)
        acknowledged = [
            cast(dict[str, object], item["evidence"])
            for item in self._completed(document, "acknowledge-inspection")
            if _request(item)["receipt_id"] == receipt_id
        ]
        if (
            not acknowledged
            or acknowledged[-1]["projection_sha256"] != inspection["projection_sha256"]
            or acknowledged[-1]["revision_id"] != inspection["revision_id"]
            or inspection["current"] is not True
        ):
            raise _Stop("inspection_required", document=document,
                        next_actions=("inspect", "acknowledge-inspection"))
        source = self._inspect_source(cast(str, inspection["source_id"]))
        if source.head_capture_id != inspection["revision_id"] or source.lifecycle != "active":
            # A newer head (edit) or withdrawal needs a new complete inspection.
            raise _Stop("revision_conflict", document=document, next_actions=("inspect",))
        return {
            "receipt_id": receipt_id,
            "source_id": source.source_id,
            "head": source.head_capture_id,
            "head_version": source.head_version,
            "route_version": source.route_version,
            "lifecycle_version": source.lifecycle_version,
        }

    @staticmethod
    def _source_refs(member: Mapping[str, object]) -> list[dict[str, object]]:
        source_id = cast(str, member["source_id"])
        return [
            {"kind": "source", "id": source_id, "version": cast(str, member["head"])},
            {"kind": "route", "id": source_id, "version": str(member["route_version"])},
        ]

    def _routed(self, document: Mapping[str, object], member: Mapping[str, object]) -> bool:
        """Routed by this workflow at exactly the current head and route version."""
        return any(
            cast(dict[str, object], item["evidence"])["source_id"] == member["source_id"]
            and cast(dict[str, object], item["evidence"])["head"] == member["head"]
            and cast(dict[str, object], item["evidence"])["route_version"]
            == member["route_version"]
            for item in self._completed(document, "route")
        )

    # -- route ---------------------------------------------------------------

    def _route(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        space_id = self._choices(document)["destination_space_id"]
        if space_id is None:
            raise _Stop("inspection_required", document=document,
                        next_actions=("destination", "create-destination"))
        self._identity(document)
        member = self._member(document, cast(str, decision.arguments["receipt_id"]))
        if self._routed(document, member):
            raise _Stop("invalid_request", document=document, next_actions=("select",))
        saved = self._saved(document, decision)
        request = {
            "expected_head": member["head"],
            "expected_route_version": member["route_version"],
            "operation_id": (
                cast(str, saved["operation_id"]) if saved is not None
                else "operation_" + str(uuid.uuid4())
            ),
            "source_id": member["source_id"],
            "space_id": space_id,
        }
        document, prepared = self._propose(
            document, decision, request=request, versions=self._source_refs(member),
            report=None,
            summary=(
                f"Route the inspected message {member['head']} to the dedicated Work space "
                f"{space_id}. Routing is assignment, not release; the original stays local."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._route_completed(finished, prepared, request, value)

        finished, _value = self._run(document, prepared, "route", request, complete)
        return result_envelope(
            "route", "completed", document=finished,
            data=_progress("route", source_id=cast(str, member["source_id"])),
            next_actions=("route", "consent", "select"),
        )

    def _route_completed(
        self, finished: dict[str, object], index: int, request: Mapping[str, object],
        value: object,
    ) -> None:
        receipt = cast(dict[str, object], value)
        operation = self._operations(finished)[index]
        if receipt["source_id"] != request["source_id"] or receipt["head"] != request[
            "expected_head"
        ]:
            raise GuideAdapterError("operation_unknown")
        operation["evidence"] = {
            "source_id": receipt["source_id"],
            "head": receipt["head"],
            "route_version": receipt["route_version"],
            "operation_id": request["operation_id"],
            "request_sha256": sha256_text(canonical_json(dict(request))),
        }

    # -- consent -------------------------------------------------------------

    @property
    def _consent_path(self) -> str | None:
        document = self.journal.read()
        if document is None:
            return None
        return cast(str | None, self._choices(document)["consent_state"])

    def _consent_store(self, request: Mapping[str, object]) -> Any:
        from open_brain.services.session_consent import DurableProviderConsentStore

        path = self._consent_path
        if path is None:
            raise GuideAdapterError("invalid_request")
        return DurableProviderConsentStore(
            Path(path),
            brain_id=cast(str, request["brain_id"]),
            issuer_epoch=cast(int, request["issuer_epoch"]),
        )

    def _consent_generation(self, document: Mapping[str, object]) -> tuple[str, Any]:
        """Current generation and state; an absent file is generation 0 with no records."""
        from open_brain.services.session_consent import SessionConsentError

        brain_id, issuer_epoch = self._identity(document)
        store = self._consent_store({"brain_id": brain_id, "issuer_epoch": issuer_epoch})
        try:
            state = store.load()
        except SessionConsentError as error:
            if error.code != "consent_unavailable":
                raise _Stop("authority_refused", document=document) from None
            return "absent", None
        return str(state.authorization_generation), state

    def _consent(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        from open_brain_engine.core.models import PrivacyTier

        choices = self._choices(document)
        provider = cast(str, decision.arguments["provider_id"])
        if choices["provider_id"] is None or provider != choices["provider_id"]:
            # One provider per workflow, chosen at preflight; never inferred.
            raise _Stop("invalid_request", document=document)
        brain_id, issuer_epoch = self._identity(document)
        generation, state = self._consent_generation(document)
        if state is not None and any(
            record.active and record.provider_id == provider
            and record.allowed_tiers != frozenset({PrivacyTier.WORK})
            for record in state.inspect(owner=True).records
        ):
            # Existing incompatible consent is never widened, replaced or revoked here.
            raise _Stop("authority_refused", document=document)
        saved = self._saved(document, decision)
        request = {
            "allowed_tiers": ["work"],
            "brain_id": brain_id,
            "decided_at": cast(str, saved["decided_at"]) if saved is not None else _now(),
            "issuer_epoch": issuer_epoch,
            "operation_id": (
                cast(str, saved["operation_id"]) if saved is not None
                else "guide.consent." + uuid.uuid4().hex
            ),
            "provider_id": provider,
        }
        document, prepared = self._propose(
            document, decision, request=request,
            versions=[{"kind": "consent", "id": provider, "version": generation}],
            report=None,
            summary=(
                f"Grant provider {provider} consent to receive Work-tier content only. "
                "This does not approve any message; each release is approved separately."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._consent_completed(finished, prepared, request, value)

        finished, _value = self._run(document, prepared, "consent", request, complete)
        return result_envelope(
            "consent", "completed", document=finished, data=_progress("consent"),
            next_actions=("select", "release-preview"),
        )

    def _consent_completed(
        self, finished: dict[str, object], index: int, request: Mapping[str, object],
        value: object,
    ) -> None:
        self._operations(finished)[index]["evidence"] = {
            "operation_id": request["operation_id"],
            "consent_id": cast(Any, value).consent_id,
            "request_sha256": sha256_text(canonical_json(dict(request))),
        }

    # -- selection -----------------------------------------------------------

    def _selection(self, document: Mapping[str, object]) -> dict[str, object] | None:
        completed = self._completed(document, "select")
        return None if not completed else cast(dict[str, object], completed[-1]["evidence"])

    def _select(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        space_id = self._choices(document)["destination_space_id"]
        if space_id is None:
            raise _Stop("inspection_required", document=document,
                        next_actions=("destination", "create-destination"))
        if self._selection(document) is not None:
            # A selection is never silently replaced; changed membership is a new workflow.
            raise _Stop("invalid_request", document=document, next_actions=("release-preview",))
        brain_id, issuer_epoch = self._identity(document)
        source_id = cast(str, self._configured(document))
        report = self._metadata(source_id=source_id)
        members = [
            self._member(document, receipt_id, report)
            for receipt_id in cast(list[str], decision.arguments["member_receipt_ids"])
        ]
        sources = [cast(str, member["source_id"]) for member in members]
        if len(set(sources)) != len(sources):
            raise _Stop("invalid_request", document=document)
        if not all(self._routed(document, member) for member in members):
            raise _Stop("inspection_required", document=document, next_actions=("route",))
        saved = self._saved(document, decision)
        request = {
            "brain_id": brain_id,
            "destination_space_id": space_id,
            "dto_version": 1,
            "issuer_epoch": issuer_epoch,
            "member_source_ids": sources,
            "operation_id": (
                cast(str, saved["operation_id"]) if saved is not None
                else "guide.selection." + uuid.uuid4().hex
            ),
        }
        document, prepared = self._propose(
            document, decision, request=request,
            versions=[
                {"kind": "source", "id": cast(str, member["source_id"]),
                 "version": cast(str, member["head"])}
                for member in members
            ],
            report=None,
            summary=(
                f"Create one recall selection of {len(sources)} routed messages in this exact "
                "order. Selection is not approval; each message is previewed and approved "
                "separately."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._selection_completed(finished, prepared, request, value)

        finished, value = self._run(document, prepared, "select", request, complete)
        return result_envelope(
            "select", "completed", document=finished,
            data=_progress(
                "select", count=len(sources),
                selection_id=cast(str, cast(dict[str, object], value)["selection_id"]),
            ),
            next_actions=("release-preview",),
        )

    def _selection_completed(
        self, finished: dict[str, object], index: int, request: Mapping[str, object],
        value: object,
    ) -> None:
        selection = cast(dict[str, object], value)
        if selection["member_source_ids"] != request["member_source_ids"]:
            raise GuideAdapterError("operation_unknown")
        self._operations(finished)[index]["evidence"] = {
            "selection_id": selection["selection_id"],
            "member_source_ids": list(cast(list[str], selection["member_source_ids"])),
        }

    def _configured(self, document: Mapping[str, object]) -> str | None:
        from open_brain.services.source_guide_flow import configured_source

        return configured_source(document)

    # -- release preview and approval ----------------------------------------

    @staticmethod
    def _owner_preview(subject: str, preview_id: str | None, payload: str) -> dict[str, object]:
        return {
            "kind": "owner_preview",
            "subject": subject,
            "preview_id": preview_id,
            "payload_utf8": payload,
            "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }

    def _release_preview(
        self, document: dict[str, object], decision: Decision
    ) -> dict[str, object]:
        choices = self._choices(document)
        selection = self._selection(document)
        if selection is None:
            raise _Stop("inspection_required", document=document, next_actions=("select",))
        if not self._completed(document, "consent"):
            # No implicit consent: the provider grant is its own owner checkpoint.
            raise _Stop("inspection_required", document=document, next_actions=("consent",))
        if decision.arguments["provider_ids"] != [choices["provider_id"]]:
            raise _Stop("invalid_request", document=document)
        brain_id, issuer_epoch = self._identity(document)
        member = self._member(document, cast(str, decision.arguments["receipt_id"]))
        if member["source_id"] not in cast(list[str], selection["member_source_ids"]):
            raise _Stop("invalid_request", document=document)
        if any(
            item["state"] == "captured"
            and self._previewed_head(document, cast(str, item["preview_id"])) == member["head"]
            for item in self._approved(document)
        ):
            # Already released at this exact head: no second copy, show progress instead.
            raise _Stop("invalid_request", document=document,
                        data=self._summary("release-preview", document),
                        next_actions=("policy-preview",))
        saved = self._saved(document, decision)
        request = {
            "brain_id": brain_id,
            "destination_space_id": choices["destination_space_id"],
            "dto_version": 2,
            "expected_head": member["head"],
            "expected_head_version": member["head_version"],
            "expected_lifecycle_version": member["lifecycle_version"],
            "expected_route_version": member["route_version"],
            "issuer_epoch": issuer_epoch,
            "operation_id": (
                cast(str, saved["operation_id"]) if saved is not None
                else "guide.release." + uuid.uuid4().hex
            ),
            "provider_ids": list(cast(list[str], decision.arguments["provider_ids"])),
            "selection_id": selection["selection_id"],
            "sharing_kind": "slack_work_evidence",
            "source_id": member["source_id"],
        }
        document, prepared = self._propose(
            document, decision, request=request,
            versions=[
                *self._source_refs(member),
                {"kind": "selection", "id": cast(str, selection["selection_id"]),
                 "version": sha256_text(canonical_json(selection["member_source_ids"]))},
            ],
            report=None,
            summary=(
                f"Prepare the exact external release preview of message {member['head']} for "
                f"provider {choices['provider_id']}. Nothing is shared until you inspect it and "
                "approve it separately."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._sharing_completed(finished, prepared, request, value)

        finished, value = self._run(document, prepared, "release-preview", request, complete)
        preview = cast(Any, value)
        payload = canonical_json(asdict(preview))
        return result_envelope(
            "release-preview", "completed", document=finished,
            data=self._owner_preview("sharing", preview.preview_id, payload),
            next_actions=("approve",),
        )

    def _sharing_completed(
        self, finished: dict[str, object], index: int, request: Mapping[str, object],
        value: object,
    ) -> None:
        operation = self._operations(finished)[index]
        if operation["action"] == "release-preview" or "decision" not in request:
            preview = cast(Any, value)
            if preview.source_id != request["source_id"] or preview.original_capture_id != request[
                "expected_head"
            ]:
                raise GuideAdapterError("operation_unknown")
            operation["evidence"] = {
                "operation_id": request["operation_id"], "preview_id": preview.preview_id,
                "approval_id": None, "copy_capture_id": None, "state": None,
                "receipt_sha256": None,
            }
            return
        receipt = cast(Any, value)
        if receipt.preview_id != request["preview_id"] or receipt.decision != "approve":
            raise GuideAdapterError("operation_unknown")
        operation["evidence"] = {
            "operation_id": request["operation_id"], "preview_id": receipt.preview_id,
            "approval_id": receipt.approval_id, "copy_capture_id": receipt.copy_capture_id,
            "state": receipt.state, "receipt_sha256": receipt.receipt_sha256,
        }

    def _previewed_source(self, document: Mapping[str, object], preview_id: str) -> str | None:
        for item in self._completed(document, "release-preview"):
            if cast(dict[str, object], item["evidence"])["preview_id"] == preview_id:
                return cast(str, _request(item)["source_id"])
        return None

    def _approved(self, document: Mapping[str, object]) -> list[dict[str, object]]:
        return [cast(dict[str, object], item["evidence"])
                for item in self._completed(document, "approve")]

    def _approve(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        from open_brain_engine.engine.sharing_contracts import SharingInspectRequest

        preview_id = cast(str, decision.arguments["preview_id"])
        source_id = self._previewed_source(document, preview_id)
        if source_id is None:
            raise _Stop("inspection_required", document=document,
                        next_actions=("release-preview",))
        if any(item["preview_id"] == preview_id for item in self._approved(document)):
            raise _Stop("invalid_request", document=document)
        brain_id, issuer_epoch = self._identity(document)
        try:
            with self._owner() as (tasks, owner):
                inspection = tasks.sharing.inspect(
                    SharingInspectRequest(subject_id=preview_id), authority=owner
                )
        except Exception as error:
            raise _Stop(_classify(error, action="approve").code, document=document) from None
        preview = inspection.preview
        if inspection.decision is not None:
            raise _Stop("authority_refused", document=document)
        if datetime.fromisoformat(preview.expires_at) <= datetime.now(UTC):
            # Expired previews never auto-authorize; a new inspected preview is new intent.
            raise _Stop("authority_refused", document=document,
                        next_actions=("release-preview",))
        source = self._inspect_source(source_id)
        if source.head_capture_id != preview.original_capture_id:
            # An edited message needs its own inspection and a new release preview.
            raise _Stop("revision_conflict", document=document,
                        next_actions=("inspect", "release-preview"))
        saved = self._saved(document, decision)
        request = {
            "brain_id": brain_id,
            "decision": "approve",
            "destination_brain_id": brain_id,
            "dto_version": 1,
            "expected_decision_version": 0,
            "issuer_epoch": issuer_epoch,
            "operation_id": (
                cast(str, saved["operation_id"]) if saved is not None
                else "guide.approve." + uuid.uuid4().hex
            ),
            "preview_id": preview_id,
            "preview_sha256": preview.preview_sha256,
        }
        payload = canonical_json(asdict(preview))
        document, prepared = self._propose(
            document, decision, request=request,
            versions=[{"kind": "source", "id": source_id, "version": source.head_capture_id}],
            report=None,
            summary=(
                f"Approve exactly this external release of message {preview.original_capture_id} "
                f"to provider(s) {', '.join(preview.provider_ids)}. The full payload is shown "
                "with this intent; approval creates one separately captured copy."
            ),
            data=self._owner_preview("sharing", preview_id, payload),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            self._sharing_completed(finished, prepared, request, value)

        finished, _value = self._run(document, prepared, "approve", request, complete)
        return result_envelope(
            "approve", "completed", document=finished,
            data=self._summary("approve", finished),
            next_actions=("release-preview", "policy-preview"),
        )

    def _summary(self, phase: str, document: Mapping[str, object]) -> dict[str, object]:
        """Approved copy IDs plus how many selected members have a current captured copy."""
        selection = self._selection(document)
        approved = self._approved(document)
        copies = [cast(str, item["copy_capture_id"]) for item in approved
                  if item["copy_capture_id"] is not None]
        ready = 0
        if selection is not None:
            from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest

            members = cast(list[str], selection["member_source_ids"])
            with self._owner() as (tasks, owner):
                heads = [
                    tasks.sources.inspect(
                        SourceInspectRequest(source_id=source_id), authority=owner
                    ).head_capture_id
                    for source_id in members
                ]
            for head in heads:
                ready += any(
                    item["state"] == "captured"
                    and self._previewed_head(document, cast(str, item["preview_id"])) == head
                    for item in approved
                )
        return _progress(
            phase,
            count=ready,
            selection_id=None if selection is None else cast(str, selection["selection_id"]),
            approved_copy_ids=copies[:24],
        )

    def _previewed_head(self, document: Mapping[str, object], preview_id: str) -> str | None:
        for item in self._completed(document, "release-preview"):
            if cast(dict[str, object], item["evidence"])["preview_id"] == preview_id:
                return cast(str, _request(item)["expected_head"])
        return None

    # -- scoped policy -------------------------------------------------------

    @staticmethod
    def _policy_inputs(values: Mapping[str, object]) -> Any:
        from open_brain.services.scoped_policy_authoring import PolicyInputs

        return PolicyInputs(
            workflow_id=cast(str, values["workflow_id"]),
            destination_space_id=cast(str, values["destination_space_id"]),
            provider_id=cast(str, values["provider_id"]),
            consent_id=cast(str, values["consent_id"]),
            authorization_generation=cast(int, values["authorization_generation"]),
            brain_id=cast(str, values["brain_id"]),
            issuer_epoch=cast(int, values["issuer_epoch"]),
        )

    def _policy_request(
        self, document: Mapping[str, object], action: str
    ) -> tuple[dict[str, object], Any]:
        """Current non-content policy inputs and the ephemeral full preview."""
        from open_brain.services.scoped_policy_authoring import (
            PolicyAuthoringError,
            current_work_consent,
            preview_scoped_policy,
        )

        choices = self._choices(document)
        if choices["destination_space_id"] is None or not self._completed(document, "consent"):
            raise _Stop("inspection_required", document=document,
                        next_actions=("create-destination", "consent"))
        self._require_dedicated(document, action)
        brain_id, issuer_epoch = self._identity(document)
        consent_path = Path(cast(str, choices["consent_state"]))
        try:
            consent_id, generation = current_work_consent(
                consent_path, provider_id=cast(str, choices["provider_id"]),
                brain_id=brain_id, issuer_epoch=issuer_epoch,
            )
            request: dict[str, object] = {
                "authorization_generation": generation,
                "brain_id": brain_id,
                "consent_id": consent_id,
                "destination_space_id": choices["destination_space_id"],
                "issuer_epoch": issuer_epoch,
                "policy_path": choices["policy_path"],
                "provider_id": choices["provider_id"],
                "workflow_id": document["workflow_id"],
            }
            preview = preview_scoped_policy(
                Path(cast(str, choices["policy_path"])), self._policy_inputs(request),
                consent_path=consent_path,
            )
        except PolicyAuthoringError as error:
            raise _Stop(error.code, document=document) from None
        request["expected_absent"] = preview.expected_absent
        return request, preview

    def _policy_preview(
        self, document: dict[str, object], decision: Decision
    ) -> dict[str, object]:
        request, preview = self._policy_request(document, "policy-preview")
        # Only non-content inputs and the SHA enter the journal; the body stays ephemeral.
        updated = self._record(
            document, "policy-preview", request,
            evidence={"policy_sha256": preview.policy_sha256,
                      "path": cast(str, request["policy_path"])},
        )
        return result_envelope(
            "policy-preview", "completed", document=updated,
            data=self._owner_preview("policy", None, preview.payload.decode("utf-8")),
            next_actions=("policy-apply",),
        )

    def _policy_apply(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        wanted = cast(str, decision.arguments["policy_sha256"])
        previews = self._completed(document, "policy-preview")
        if not previews or cast(dict[str, object], previews[-1]["evidence"])[
            "policy_sha256"
        ] != wanted:
            raise _Stop("inspection_required", document=document,
                        next_actions=("policy-preview",))
        current, preview = self._policy_request(document, "policy-apply")
        # Regenerated from the confirmed inputs: any drift needs a new full inspection.
        if current != _request(previews[-1]) or preview.policy_sha256 != wanted:
            raise _Stop("revision_conflict", document=document, next_actions=("policy-preview",))
        request = {**current, "policy_sha256": wanted}
        document, prepared = self._propose(
            document, decision, request=request,
            versions=[
                {"kind": "consent", "id": cast(str, current["consent_id"]),
                 "version": str(current["authorization_generation"])},
                {"kind": "policy", "id": cast(str, current["policy_path"]), "version": wanted},
            ],
            report=None,
            summary=(
                "Create the inspected scoped policy file (search and content-read on one Work "
                "space, one provider). Create-only: an existing file is never overwritten."
                if current["expected_absent"]
                else "Use the existing policy file whose exact scope you inspected. No write."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], _value: object) -> None:
            self._operations(finished)[prepared]["evidence"] = {
                "policy_sha256": wanted, "path": cast(str, current["policy_path"]),
            }

        finished, _value = self._run(document, prepared, "policy-apply", request, complete)
        return result_envelope(
            "policy-apply", "completed", document=finished,
            data=_progress("policy-apply", policy_sha256=wanted),
            next_actions=("setup-preview",),
        )

    # -- scoped setup --------------------------------------------------------

    def _setup(self, mode: str, values: Mapping[str, object]) -> dict[str, object]:
        from open_brain.local_data import select_local_root
        from open_brain.services.agent_setup import apply_agent_setup, preview_agent_setup

        selection = select_local_root(
            data_dir=str(self.paths.brain), environment=self.environment
        )
        # Only search and content-read; every other grant stays false. No client launch.
        arguments: dict[str, object] = {
            "client": values["client"],
            "scope": values["scope"],
            "project_dir": values["project_dir"],
            "allow_capture": False,
            "allow_search": True,
            "allow_content_read": True,
            "action": "configure",
            "runtime_path": Path(cast(str, values["runtime_path"])),
            "environment": self.environment,
            "session_policy": values["session_policy"],
            "consent_state": values["consent_state"],
            "client_version": values["client_version"],
            "launch_context": values["launch_context"],
        }
        if mode == "preview":
            return preview_agent_setup(selection, **cast(Any, arguments))
        return apply_agent_setup(
            selection, **cast(Any, arguments), preview_id=values["preview_id"]
        )

    def _setup_request(self, document: Mapping[str, object]) -> dict[str, object]:
        from open_brain.services.scoped_policy_authoring import (
            PolicyAuthoringError,
            read_existing_policy,
        )

        choices = self._choices(document)
        applied = self._completed(document, "policy-apply")
        if not applied:
            raise _Stop("inspection_required", document=document, next_actions=("policy-apply",))
        evidence = cast(dict[str, object], applied[-1]["evidence"])
        try:
            current = read_existing_policy(Path(cast(str, choices["policy_path"])))
        except PolicyAuthoringError:
            current = None
        if current is None or hashlib.sha256(current).hexdigest() != evidence["policy_sha256"]:
            # A changed or removed policy is stale; it is never repaired here.
            raise _Stop("revision_conflict", document=document, next_actions=("policy-preview",))
        return {
            "client": choices["client_kind"],
            "client_version": choices["client_version"],
            "consent_state": choices["consent_state"],
            "launch_context": choices["launch_context"],
            "policy_sha256": evidence["policy_sha256"],
            "project_dir": choices["context_path"] if choices["scope"] == "project" else None,
            "runtime_path": choices["runtime_path"],
            "scope": choices["scope"],
            "session_policy": choices["policy_path"],
        }

    def _setup_versions(
        self, document: Mapping[str, object], request: Mapping[str, object]
    ) -> list[dict[str, object]]:
        generation, _state = self._consent_generation(document)
        return [
            {"kind": "policy", "id": cast(str, request["session_policy"]),
             "version": cast(str, request["policy_sha256"])},
            {"kind": "consent", "id": cast(str, self._choices(document)["provider_id"]),
             "version": generation},
        ]

    @staticmethod
    def _setup_payload(preview: Mapping[str, object]) -> str:
        """Only the proposed Open Brain owned fragments and grants, never unrelated config."""
        return canonical_json({
            key: preview[key]
            for key in ("action", "brain_root", "changes", "client", "permissions",
                        "preview_id", "runtime_path", "scope")
        })

    def _setup_preview(
        self, document: dict[str, object], decision: Decision
    ) -> dict[str, object]:
        self._identity(document)
        request = self._setup_request(document)
        document, prepared = self._propose(
            document, decision, request=request,
            versions=self._setup_versions(document, request), report=None,
            summary=(
                f"Compute the {request['client']} {request['scope']} setup preview with search "
                "and content-read only, bound to the inspected policy and current consent. "
                "Nothing is written and no client is launched."
            ),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            preview = cast(dict[str, object], value)
            self._operations(finished)[prepared]["evidence"] = {
                "preview_id": preview["preview_id"], "status": "preview",
                "changed_path_count": len(cast(list[object], preview["changes"])),
            }

        finished, value = self._run(document, prepared, "setup-preview", request, complete)
        preview = cast(dict[str, object], value)
        return result_envelope(
            "setup-preview", "completed", document=finished,
            data=self._owner_preview(
                "setup", cast(str, preview["preview_id"]), self._setup_payload(preview)
            ),
            next_actions=("setup-apply",),
        )

    def _setup_apply(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        preview_id = cast(str, decision.arguments["preview_id"])
        previews = self._completed(document, "setup-preview")
        if not previews or cast(dict[str, object], previews[-1]["evidence"])[
            "preview_id"
        ] != preview_id:
            raise _Stop("inspection_required", document=document, next_actions=("setup-preview",))
        self._identity(document)
        self._require_dedicated(document, "setup-apply")
        base = self._setup_request(document)
        if base != _request(previews[-1]):
            raise _Stop("revision_conflict", document=document, next_actions=("setup-preview",))
        try:
            current = self._setup("preview", base)
        except Exception as error:
            raise _Stop(_classify(error, action="setup-preview").code,
                        document=document) from None
        if current["preview_id"] != preview_id:
            # Current configuration, policy or consent changed: repreview, never replay.
            raise _Stop("revision_conflict", document=document, next_actions=("setup-preview",))
        request = {**base, "preview_id": preview_id}
        document, prepared = self._propose(
            document, decision, request=request,
            versions=self._setup_versions(document, base), report=None,
            summary=(
                "Apply exactly the displayed Open Brain owned setup fragments. Unrelated "
                "settings are preserved; the client and model are not launched."
            ),
            data=self._owner_preview("setup", preview_id, self._setup_payload(current)),
        )
        if isinstance(prepared, dict):
            return prepared

        def complete(finished: dict[str, object], value: object) -> None:
            result = cast(dict[str, object], value)
            self._operations(finished)[prepared]["evidence"] = {
                "preview_id": preview_id, "status": result["status"],
                "changed_path_count": len(cast(list[object], result["changes"])),
            }

        finished, _value = self._run(document, prepared, "setup-apply", request, complete)
        return result_envelope(
            "setup-apply", "completed", document=finished,
            data=self._summary("setup-apply", finished),
        )

    # -- reconcile -----------------------------------------------------------

    def _reconcile(self, document: dict[str, object], decision: Decision) -> dict[str, object]:
        sequence = cast(int, decision.arguments["operation_sequence"])
        operations = self._operations(document)
        if sequence >= len(operations) or sequence not in self._unresolved(document):
            raise _Stop("invalid_request", document=document)
        target = operations[sequence]
        action = cast(str, target["action"])
        request = {"operation_sequence": sequence, "request_sha256": target["request_sha256"]}
        if action in _REPREVIEW:
            # HALT AND REPREVIEW: no authority call, no replay of a stale preview or create.
            updated = self._record(document, "reconcile", request, state="refused")
            raise _Stop("operation_unknown", document=updated,
                        next_actions=(_REPREVIEW[action],))
        if action not in _REPLAYABLE:
            return super()._reconcile(document, decision)
        saved = _request(target)
        document, prepared = self._propose(
            document, decision, request=request, versions=(), report=None,
            summary=(
                f"Replay the exact saved {action} request (operation sequence {sequence}). The "
                "owner authority returns its existing receipt or refuses; no new operation ID "
                "is created."
            ),
        )
        if isinstance(prepared, dict):
            return prepared
        finished = self._replay(document, prepared, sequence, action, saved)
        return result_envelope(
            "reconcile", "completed", document=finished,
            data=self._summary("reconcile", finished) if action == "approve"
            else _progress("reconcile"),
            next_actions=self._after(action),
        )

    @staticmethod
    def _after(action: str) -> Sequence[str]:
        return {
            "create-destination": ("route",),
            "route": ("route", "consent", "select"),
            "consent": ("select", "release-preview"),
            "select": ("release-preview",),
            "release-preview": ("approve",),
            "approve": ("release-preview", "policy-preview"),
        }[action]

    def _replay(
        self,
        document: dict[str, object],
        prepared: int,
        sequence: int,
        action: str,
        saved: Mapping[str, object],
    ) -> dict[str, object]:
        completions: dict[str, Callable[[dict[str, object], int, Mapping[str, object], object],
                                        None]] = {
            "create-destination": self._space_completed,
            "route": self._route_completed,
            "consent": self._consent_completed,
            "select": self._selection_completed,
            "release-preview": self._sharing_completed,
            "approve": self._sharing_completed,
        }

        def call(_guard: int) -> dict[str, object]:
            return {"value": self._authority(action, saved)}

        def complete(finished: dict[str, object], value: dict[str, object]) -> None:
            completions[action](finished, sequence, saved, value["value"])
            self._operations(finished)[sequence]["state"] = "completed"

        finished, _value, failure = self._execute(document, prepared, call, complete)
        if failure is not None:
            if failure.code == "operation_unknown":
                raise _Stop("operation_unknown", document=finished, next_actions=("reconcile",))
            # A refused replay never proves the earlier attempt did not commit.
            raise _Stop(failure.code, document=self._halt(finished), next_actions=("cancel",))
        return finished
