"""Saved-content checkpointing with no capture or ordinary recovery capability."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from open_brain_engine.engine import CaptureSubmission, PublicJobCaptureContext
from open_brain_engine.engine.historical_root_checkpoint import (
    HistoricalRootCheckpointCapability,
    HistoricalRootCheckpointHost,
    HistoricalRootContinuity,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain_collector.lifecycle import SavedMarkdownBaseline
from open_brain_collector.runner import _open_existing_collector_profile
from open_brain_connectors.runtime.source_intake import SourceRecordIntake


class HistoricalSavedCheckpointSink:
    """Reconstruct incoming content using exact original historical coordinates."""

    def __init__(
        self, host: HistoricalRootCheckpointHost, context: PublicJobCaptureContext
    ) -> None:
        self._host = host
        self._context = context

    @property
    def brain_identity(self) -> tuple[str, int]:
        destination = self._host.continuity.destination
        return destination.brain_id, destination.issuer_epoch

    @property
    def brain_binding(self) -> str:
        return self._host.continuity.fingerprint(self._host.profile.root_identity)

    def _capability(self, intake: SourceRecordIntake) -> HistoricalRootCheckpointCapability:
        if type(intake) is not SourceRecordIntake or intake.key.connector_name != "saved_markdown":
            raise T03Error("invalid_arguments")
        namespace = {
            key: getattr(intake.key, key)
            for key in ("connector_name", "connection_id", "resource_id", "external_id")
        }
        return self._host.capability(
            SourceRevisionBinding(
                destination_brain_id=self.brain_identity[0],
                issuer_epoch=self.brain_identity[1],
                root_fingerprint=self.brain_binding,
                accepted_source_id=intake.key.connection_id,
                namespace=namespace,
            )
        )

    def lookup_baseline(
        self,
        intake: SourceRecordIntake,
        *,
        selection_generation: str,
        expected_capture_id: str | None = None,
    ) -> SavedMarkdownBaseline | None:
        capability = self._capability(intake)
        if intake.observation is None:
            return None
        template = capability.baseline_template()
        if template is None or (
            expected_capture_id is not None and expected_capture_id != template.expected_head
        ):
            return None
        capture = CaptureSubmission.for_public_job(
            context=self._context,
            payload=intake.payload(),
            delivery_id=template.capture_delivery_id,
            source_origin="third_party",
            source_reference=intake.source_reference,
            provenance=intake.provenance(),
            privacy=intake.privacy,
            intent="reference",
            title=intake.title,
        )
        binding = capability.binding
        delivery = SourceRevisionObservedDelivery(
            binding=binding,
            submission=SourceRevisionSubmission(
                capture=capture,
                namespace=binding.namespace,
                revision_key=template.revision_key,
                canonical_sha256=capture.request_sha256(),
                expected_head=template.expected_head,
                ordering=template.ordering,
                expected_control_epoch=template.expected_control_epoch,
            ),
            expected_lifecycle_version=template.expected_lifecycle_version,
            delivery_id=template.delivery_id,
            observation=intake.observation,
        )
        if delivery.envelope_sha256 != template.observed_envelope_sha256:
            return None
        result = capability.lookup_baseline(delivery, selection_generation=selection_generation)
        return None if result is None else SavedMarkdownBaseline(delivery, result)

    @contextmanager
    def page_checkpoint(
        self,
        items: tuple[tuple[SourceRecordIntake, str], ...],
        *,
        selection_generation: str,
    ) -> Iterator[tuple[SavedMarkdownBaseline, ...]]:
        if type(items) is not tuple or not 0 <= len(items) <= 25:
            raise T03Error("invalid_arguments")
        if not items:
            with self._host.empty_page_checkpoint(selection_generation=selection_generation):
                yield ()
            return
        terminals = []
        for intake, capture_id in items:
            baseline = self.lookup_baseline(
                intake, selection_generation=selection_generation, expected_capture_id=capture_id
            )
            if baseline is None or baseline.result.capture_id != capture_id:
                raise T03Error("revision_changed")
            terminals.append(baseline)
        capability = self._capability(items[0][0])
        witnesses = tuple((item.delivery, item.result) for item in terminals)
        with capability.revision_page_checkpoint(
            witnesses, selection_generation=selection_generation
        ):
            yield tuple(terminals)


def collector_historical_checkpoint_sink(
    brain_root: Path,
    continuity: HistoricalRootContinuity,
    *,
    authority: EffectiveAuthority,
    validate_continuity: Callable[[], None],
) -> HistoricalSavedCheckpointSink:
    """Open only a read-only history host; never open the ordinary engine.

    The trusted owner caller validates independently retained continuity evidence
    and the current physical admission fence on every validator call. This API
    never treats a prior fingerprint or matching inode as evidence on its own.
    """
    profile = _open_existing_collector_profile(brain_root)
    host = HistoricalRootCheckpointHost(
        profile, continuity, authority=authority, validate_continuity=validate_continuity
    )
    actor = "actor_11111111-1111-4111-8111-111111111111"
    context = PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_11111111-1111-4111-8111-111111111111",
            "role_id": "role_11111111-1111-4111-8111-111111111111",
            "tenant_id": profile.tenant_id,
        },
    )
    return HistoricalSavedCheckpointSink(host, context)
