"""The outbox transport protocol and the in-process synthetic transport.

``Transport`` keeps its single definition in ``drain`` (O3) and is
re-exported here as the transport module's public name, so exactly one
protocol shape exists. ``SyntheticTransport`` is the only transport the
automated suite exercises: it submits straight into a disposable engine
task set. The destination-bound submission is rebuilt from the immutable
envelope alone -- the text family is the only family an outbox envelope
admits (O1) -- under an ``EffectiveAuthority`` compiled once at
construction from a ``SyntheticStartupPolicy``. Validating a startup policy
against a durable Brain identity is app-owned launcher territory and is
deliberately absent: connectors may not import the app package. The
synthetic policy is test input, and the drain's ``verify_terminal_receipt``
remains the binding safety net for wrong-Brain or stale-epoch authorities.

Outcomes follow OSS-G4 exactly: a ``CaptureAdmissionError`` keeps its
stable result value and retryable flag, a ``DeliveryConflict`` is one
terminal code, and a receipt missing its destination-bound binding is a
terminal ``receipt_malformed`` refusal. Nothing is retried or quarantined
here; classification stays the drain's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureAdmissionError,
    CaptureSubmission,
    DeliveryConflict,
    EngineTaskSet,
    TextPayload,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from .contracts import DeliveryEnvelope, OutboxContractError, TerminalReceipt
from .destination import (
    RECEIPT_MALFORMED,
    delivery_conflict_failure,
    failure_from_admission_error,
    terminal_receipt_from_capture_receipt,
)
from .drain import DeliveryFailure, Transport

__all__ = [
    "OutboxTransportError",
    "SyntheticStartupPolicy",
    "SyntheticTransport",
    "Transport",
]

_SYNTHETIC_SESSION_ID = "outbox-synthetic-session"
_SYNTHETIC_CAPABILITIES = frozenset({"capture-accept"})


class OutboxTransportError(ValueError):
    """A transport was constructed or called outside its contract."""


@dataclass(frozen=True, slots=True)
class SyntheticStartupPolicy:
    """The synthetic stand-in for a deployment's validated startup policy.

    Carries exactly the fields the destination-bound path consumes from an
    authority -- destination Brain, issuer epoch, capture-tier allowlist,
    principal -- plus the tenant the profile must match, so a test-built
    authority is deterministic and needs no app-package policy machinery.
    """

    destination_brain_id: str
    issuer_epoch: int
    tenant_id: str
    principal_id: str
    allowed_capture_tiers: frozenset[PrivacyTier]

    def __post_init__(self) -> None:
        for value in (self.destination_brain_id, self.tenant_id, self.principal_id):
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 256
                or any(ord(character) < 33 or ord(character) == 127 for character in value)
            ):
                raise OutboxTransportError("invalid synthetic policy reference")
        if type(self.issuer_epoch) is not int or self.issuer_epoch < 1:
            raise OutboxTransportError("invalid synthetic policy issuer epoch")
        if type(self.allowed_capture_tiers) is not frozenset or not all(
            isinstance(tier, PrivacyTier) for tier in self.allowed_capture_tiers
        ):
            raise OutboxTransportError("invalid synthetic policy capture tiers")


class SyntheticTransport:
    """In-process transport submitting each delivery into a real engine."""

    def __init__(self, tasks: EngineTaskSet, policy: SyntheticStartupPolicy) -> None:
        if not isinstance(tasks, EngineTaskSet):
            raise OutboxTransportError("invalid engine task set")
        if not isinstance(policy, SyntheticStartupPolicy):
            raise OutboxTransportError("invalid synthetic startup policy")
        if policy.tenant_id != tasks.profile.tenant_id:
            # The profile tenant enters the request digest, so a mismatched
            # policy could only ever produce receipts that fail verification.
            raise OutboxTransportError("synthetic policy tenant does not match the profile")
        self._profile = tasks.profile
        self._capture = tasks.capture
        try:
            self._authority = EffectiveAuthority(
                principal_id=policy.principal_id,
                session_id=_SYNTHETIC_SESSION_ID,
                capabilities=_SYNTHETIC_CAPABILITIES,
                space_ids=None,
                allowed_capture_tiers=policy.allowed_capture_tiers,
                brain_id=policy.destination_brain_id,
                issuer_epoch=policy.issuer_epoch,
            )
        except ValueError as error:
            raise OutboxTransportError("invalid synthetic startup policy") from error

    def __call__(self, envelope: DeliveryEnvelope, /) -> TerminalReceipt | DeliveryFailure:
        if not isinstance(envelope, DeliveryEnvelope):
            raise OutboxTransportError("invalid delivery envelope")
        payload = envelope.payload
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"family", "text"}
            or payload["family"] != "text"
            or not isinstance(payload["text"], str)
        ):
            raise OutboxTransportError("invalid envelope payload family")
        try:
            submission = CaptureSubmission.for_destination_bound(
                profile=self._profile,
                authority=self._authority,
                payload=TextPayload(payload["text"]),
                delivery_id=envelope.delivery_id,
                requested_tier=envelope.requested_tier,
            )
            receipt = self._capture.submit(submission)
        except CaptureAdmissionError as error:
            return failure_from_admission_error(error)
        except DeliveryConflict:
            return delivery_conflict_failure()
        try:
            return terminal_receipt_from_capture_receipt(receipt)
        except OutboxContractError:
            return DeliveryFailure(code=RECEIPT_MALFORMED, retryable=False)
