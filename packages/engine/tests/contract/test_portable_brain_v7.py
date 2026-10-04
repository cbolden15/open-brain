"""Portable 7 requires a closed sharing authority sidecar."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import local_schema
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH, validate_portable_file_set_v7
from open_brain_engine.portable.versioned import validated_portable_snapshot

from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def test_portable_v7_requires_closed_sharing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build the historical format from its actual schema, not a current export
    # with newer mandatory sidecars removed to imitate an older artifact.
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 12)
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:12])
        tasks, _, _, _ = _managed_source(tmp_path)
        export = tmp_path / "export"
        tasks.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_version"] == 7
    files = {
        path: data for path, data in snapshot.files.items() if path != "portable-manifest.json"
    }
    validate_portable_file_set_v7(files, tenant_id=tasks.profile.tenant_id)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v7(
            {path: data for path, data in files.items() if path != SHARING_APPROVALS_PATH},
            tenant_id=tasks.profile.tenant_id,
        )
    changed = dict(files)
    value = json.loads(changed[SHARING_APPROVALS_PATH])
    value["unexpected"] = True
    changed[SHARING_APPROVALS_PATH] = canonical(value)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v7(changed, tenant_id=tasks.profile.tenant_id)
