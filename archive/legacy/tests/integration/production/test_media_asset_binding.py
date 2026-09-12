from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from open_brain_engine.capture.models import ExtractionFailure
from open_brain_engine.core.models import RawAssetRef

from open_brain_legacy._compat.open_brain_connectors.capture.media import collect_staged_media
from open_brain_legacy.production.assets import ContentAddressedRawAssetStore


def test_staged_media_is_persisted_before_ephemeral_stage_is_removed(tmp_path: Path) -> None:
    objects = tmp_path / "objects"
    objects.mkdir()
    store = ContentAddressedRawAssetStore(root=objects, enabled=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    payload = b"synthetic-image-bytes"
    (stage / "synthetic.png").write_bytes(payload)

    refs, failure = collect_staged_media(stage, asset_store=store)

    assert failure is None
    assert len(refs) == 1
    assert refs[0].media_type == "image/png"
    assert store.read(refs[0]) == payload
    assert not stage.exists()


class _MismatchedStore:
    def put(self, *, data: bytes, media_type: str) -> RawAssetRef:
        del data
        digest = sha256(b"different").hexdigest()
        return RawAssetRef.create(
            asset_id="asset_" + digest,
            sha256=digest,
            media_type=media_type,
            byte_length=len(b"different"),
        )


def test_staged_media_rejects_non_verifying_asset_store_result(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "synthetic.png").write_bytes(b"synthetic-image-bytes")

    refs, failure = collect_staged_media(stage, asset_store=_MismatchedStore())

    assert refs == ()
    assert failure is ExtractionFailure.MALFORMED_TOOL_OUTPUT
    assert not stage.exists()
