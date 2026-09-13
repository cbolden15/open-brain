"""App-owned appliance composition for init/status and read-only MCP."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from open_brain_engine.engine import (
    EngineTaskSet,
    PageResult,
    ScopedRetrievalTask,
    open_authoritative_local_engine,
    open_local_read_view,
)

from open_brain.cli._common import CommandFamilyAdapter
from open_brain.cli.phase1 import build_phase1_command_adapters
from open_brain.integrations.mcp import EngineMcpAdapter
from open_brain.integrations.phase1_ui import BrowserSessionStore, Phase1UiHandler
from open_brain.integrations.ports import (
    PageDocument,
    PageReadRequest,
    RedactedText,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    TrustLabel,
)
from open_brain.profile import open_existing_single_user_local

from .appliance_recovery import ApplianceRecoveryService

if TYPE_CHECKING:
    from .appliance_scheduler import ApplianceScheduler


class ApplianceRetrievalFeedback:
    """Metadata-only feedback acknowledgement for the offline appliance MCP view."""

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        if not isinstance(request, RetrievalFeedbackRequest):
            raise ValueError("invalid retrieval feedback")
        return RetrievalFeedbackReceipt(
            retrieval_id=request.retrieval_id,
            outcome=request.outcome,
            result_count=len(request.result_ids),
            recorded=False,
        )


@dataclass(frozen=True, slots=True)
class ApplianceApplication:
    root: Path = field(repr=False)
    retrieval: ScopedRetrievalTask
    feedback: ApplianceRetrievalFeedback = field(default_factory=ApplianceRetrievalFeedback)
    mutations: EngineTaskSet | None = field(default=None, init=False)

    @classmethod
    def open_read_only(
        cls,
        root: Path,
        *,
        allowed_space_ids: frozenset[str] = frozenset(),
    ) -> ApplianceApplication:
        profile = open_existing_single_user_local(root)
        retrieval = open_local_read_view(profile, allowed_space_ids=allowed_space_ids)
        return cls(root=profile.root, retrieval=retrieval)

    @classmethod
    def open_mutating(
        cls,
        root: Path,
        authority: object | None = None,
    ) -> ApplianceApplication:
        profile = open_existing_single_user_local(root)
        mutations = open_authoritative_local_engine(profile, authority)
        application = cls(root=profile.root, retrieval=mutations.retrieval)
        object.__setattr__(application, "mutations", mutations)
        return application

    def mcp_adapter(self, *, allowed_space_ids: frozenset[str] = frozenset()) -> EngineMcpAdapter:
        retrieval = self.retrieval
        if self.mutations is not None:
            retrieval = self.mutations.retrieval.scoped(allowed_space_ids=allowed_space_ids)
        return EngineMcpAdapter(retrieval=retrieval, feedback=self.feedback)

    def cli_adapter(self, command: str) -> CommandFamilyAdapter | None:
        mutations = self.mutations
        if mutations is None:
            return None
        return build_phase1_command_adapters(mutations.phase1).get(command)

    def ui_handler(
        self,
        *,
        browser_sessions: BrowserSessionStore,
        allowed_origin: str,
        status_reader: Callable[[], dict[str, object]],
        history_reader: Callable[[int], dict[str, object]],
    ) -> Phase1UiHandler:
        mutations = self.mutations
        if mutations is None:
            raise RuntimeError("appliance UI requires a mutating application")
        return Phase1UiHandler(
            tasks=mutations.phase1,
            browser_sessions=browser_sessions,
            allowed_origin=allowed_origin,
            page_reader=self.page_reader(),
            status_reader=status_reader,
            history_reader=history_reader,
        )

    def page_reader(self) -> _AppliancePageReader:
        return _AppliancePageReader(self.retrieval)

    def recovery(
        self,
        *,
        scheduler: ApplianceScheduler | None = None,
    ) -> ApplianceRecoveryService:
        return ApplianceRecoveryService(
            self.root,
            self,
            scheduler=scheduler,
        )


@dataclass(frozen=True, slots=True)
class _AppliancePageReader:
    retrieval: ScopedRetrievalTask

    def read(self, request: PageReadRequest) -> PageDocument | None:
        if not isinstance(request, PageReadRequest):
            raise ValueError("invalid page read request")
        page = self.retrieval.read_page(request.page_id)
        if page is None:
            return None
        if not isinstance(page, PageResult):
            raise ValueError("invalid page read request")
        return PageDocument(
            page_id=request.page_id,
            title=RedactedText.redact(page.title),
            markdown=RedactedText.redact(page.markdown),
            trust=_public_page_trust(page.trust),
        )


def _public_page_trust(value: str) -> TrustLabel:
    if value in {"owner", "reviewed"}:
        return TrustLabel.VERIFIED_WORK
    if value == "third_party":
        return TrustLabel.UNREVIEWED_THIRD_PARTY
    raise ValueError("invalid page read request")
