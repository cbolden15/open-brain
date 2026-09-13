"""App-owned composition for the retained local Phase 1 surface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from open_brain_engine.engine import (
    EngineTaskSet,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    open_local_engine,
)

from open_brain.capture.http import BodyReader, ShareHttpHandler
from open_brain.cli.phase1 import build_phase1_command_adapters
from open_brain.cli.phase1_registry import Phase1CommandAdapterRegistry
from open_brain.integrations.mcp import EngineMcpAdapter
from open_brain.integrations.phase1_ui import Phase1UiHandler
from open_brain.integrations.ports import RetrievalFeedbackReceipt, RetrievalFeedbackRequest
from open_brain.profile import compile_single_user_local
from open_brain.services.runtime import ApplianceControlPlane, reserved_appliance_control_plane


class Phase1RetrievalFeedback:
    """Deliberately metadata-only acknowledgement for the public MCP representation."""

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
class SingleUserLocalApplication:
    """One app root retaining full engine tasks privately from representations."""

    tasks: EngineTaskSet
    feedback: Phase1RetrievalFeedback = field(default_factory=Phase1RetrievalFeedback)

    @classmethod
    def open(cls, root: Path) -> SingleUserLocalApplication:
        return cls(tasks=open_local_engine(compile_single_user_local(root)))

    def cli_adapters(self) -> Phase1CommandAdapterRegistry:
        return build_phase1_command_adapters(self.tasks.phase1)

    def appliance_control_plane(self) -> ApplianceControlPlane:
        return reserved_appliance_control_plane(self.tasks.daemon_mutation_path)

    def ui_handler(self, expected_bearer_token: str) -> Phase1UiHandler:
        return Phase1UiHandler(expected_bearer_token=expected_bearer_token, tasks=self.tasks.phase1)

    def share_handler(
        self,
        *,
        expected_bearer_token: str,
        body_reader: BodyReader,
        clock: Callable[[], datetime],
    ) -> ShareHttpHandler:
        return ShareHttpHandler(
            expected_bearer_token=expected_bearer_token,
            capture=self.tasks.capture,
            clock=clock,
            body_reader=body_reader,
        )

    def mcp_adapter(self, *, allowed_space_ids: frozenset[str] = frozenset()) -> EngineMcpAdapter:
        return EngineMcpAdapter(
            retrieval=self.tasks.retrieval.scoped(
                allowed_space_ids=allowed_space_ids
            ),
            feedback=self.feedback,
        )

    def public_job_sink(self, job_id: str) -> PublicJobCaptureSink:
        return self.tasks.capture.public_job_sink(self.public_job_context(job_id))

    def public_job_context(self, job_id: str) -> PublicJobCaptureContext:
        identities = {
            "JOB-005": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0005",
            "JOB-027": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0027",
            "JOB-028": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0028",
            "JOB-029": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0029",
        }
        identity = identities.get(job_id)
        if identity is None:
            raise ValueError("unsupported public capture job")
        actor_id = "actor_" + identity
        return PublicJobCaptureContext.create(
            profile=self.tasks.profile,
            actor_id=actor_id,
            role_claim={
                "actor_id": actor_id,
                "capabilities": ["capture.accept"],
                "role_claim_id": "role_claim_" + identity,
                "role_id": "role_" + identity,
                "tenant_id": self.tasks.profile.tenant_id,
            },
        )


__all__ = ["Phase1RetrievalFeedback", "SingleUserLocalApplication"]
