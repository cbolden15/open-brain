"""V2 separates the core character limit from encoded historical envelope bounds."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import TextPayload
from open_brain_engine.engine.historical_admission_v2 import verify_historical_relation_payload
from open_brain_engine.engine.historical_contracts_v2 import HistoricalBaselineRequestV2
from open_brain_engine.engine.historical_observation import decode_historical_observation
from open_brain_engine.engine.sharing_contracts import SharingError

from ._historical_compatibility_fixtures import baseline_v2, portable5_import_engine


@pytest.mark.parametrize(
    "text", ["x" * 65536, "😀" * 65536, "\x01" * 65536], ids=["ascii", "utf8", "escaped"]
)
def test_v2_roundtrips_max_character_payload_and_frozen_v1_refuses_encoded_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    engine = portable5_import_engine(tmp_path, monkeypatch)
    baseline = baseline_v2(engine)
    old = baseline.observed_delivery
    capture = replace(old.submission.capture, payload=TextPayload(text))
    observed = replace(
        old,
        submission=replace(
            old.submission, capture=capture, canonical_sha256=capture.request_sha256()
        ),
        observation=replace(
            old.observation,
            transformed_sha256=sha256(text.encode()).hexdigest(),
            admitted_payload_sha256=sha256(canonical(capture.payload.to_dict())).hexdigest(),
        ),
    )
    assert len(observed.custody_bytes()) > 65536
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation(json.loads(observed.custody_bytes()))
    request = replace(baseline, observed_delivery=observed)
    assert HistoricalBaselineRequestV2.from_json(request.canonical_bytes()) == request
    privacy = capture.privacy.to_dict()
    privacy["authority"] = {"cloud": True, "external_egress": True}
    if text.startswith("x"):
        # A large unbroken token is redaction-shaped; byte support does not grant publication.
        with pytest.raises(SharingError, match="unsupported_capability"):
            verify_historical_relation_payload(
                request, {"payload": capture.payload.to_dict(), "privacy": privacy}
            )
    else:
        verify_historical_relation_payload(
            request, {"payload": capture.payload.to_dict(), "privacy": privacy}
        )


def test_v2_request_retains_independent_encoded_byte_ceiling_and_core_character_limit() -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalBaselineRequestV2.from_json(b" " * (512 * 1024 + 1))
    with pytest.raises(ValueError):
        TextPayload("😀" * 65537)


def test_v2_large_import_transition_checkpoint_and_portable_restore_use_separate_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from open_brain_engine.engine import BrainEngine, historical_transition_v2
    from open_brain_engine.engine.historical_checkpoint import HistoricalBaselineDuplicate
    from open_brain_engine.engine.historical_dispatch import VersionedHistoricalTransitionStore
    from open_brain_engine.engine.historical_observation_v2 import (
        MAX_HISTORICAL_V2_BYTES,
        decode_historical_observation_v2,
    )
    from open_brain_engine.portable.versioned import validated_portable_snapshot

    from open_brain.profile import open_existing_single_user_local

    from .test_historical_compatibility import linked_v2

    # This is a supported old export/import, not a mutation of current journal rows.
    engine = portable5_import_engine(tmp_path, monkeypatch, text="\x01" * 65536)
    linked_v2(engine)
    record = VersionedHistoricalTransitionStore(
        engine.profile.root, engine.profile.root_identity
    ).read("baseline.synthetic.import")
    baseline = record.request
    assert isinstance(baseline, HistoricalBaselineRequestV2)
    raw = baseline.canonical_bytes()
    assert 393216 < len(raw) < MAX_HISTORICAL_V2_BYTES
    # Valid JSON whitespace isolates the request byte ceiling from DTO field limits.
    at_limit = raw + b" " * (MAX_HISTORICAL_V2_BYTES - len(raw))
    assert HistoricalBaselineRequestV2.from_json(at_limit) == baseline
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalBaselineRequestV2.from_json(at_limit + b" ")
    observation = json.loads(baseline.observed_delivery.custody_bytes())
    observation["binding"]["root_fingerprint"] = "x" * MAX_HISTORICAL_V2_BYTES
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation_v2(observation)
    transition = record.canonical_bytes()
    assert historical_transition_v2._MAX_BYTES == 9 * 1024 * 1024
    assert historical_transition_v2.HistoricalTransitionV2.from_bytes(transition) == record
    with monkeypatch.context() as bounded:
        bounded.setattr(historical_transition_v2, "_MAX_BYTES", len(transition) - 1)
        with pytest.raises(SharingError):
            historical_transition_v2.HistoricalTransitionV2.from_bytes(transition)
    checkpoint = engine.sources.public_revision_sink(
        baseline.observed_delivery.binding
    ).lookup_baseline(baseline.observed_delivery, selection_generation="synthetic.max.selection")
    assert isinstance(checkpoint, HistoricalBaselineDuplicate)
    assert checkpoint.receipt.dto_version == 2
    archive = tmp_path / "large-archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    original = validated_portable_snapshot(archive)
    destination = tmp_path / "large-restored"
    engine.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    assert (
        VersionedHistoricalTransitionStore(restored.profile.root, restored.profile.root_identity)
        .read(baseline.operation_id)
        .canonical_bytes()
        == transition
    )
    assert validated_portable_snapshot(destination).files == original.files
