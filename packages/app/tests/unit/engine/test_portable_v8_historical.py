"""Required historical authority preserves truth, not fresh consent or eligibility."""

import base64
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_contracts import HistoricalCopyRelationRequest
from open_brain_engine.engine.historical_tasks import link_historical_copy, revoke_historical_copy
from open_brain_engine.engine.portable_v5_evidence import serialize_portable_v5_state
from open_brain_engine.engine.portable_v8_authority import historical_authority_sidecar
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_store import source_metadata
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH
from open_brain_engine.portable.v5 import ISSUER_MIGRATION_PATH
from open_brain_engine.portable.v6 import (
    AUTHORITY_COLUMNS,
    SOURCE_ADMISSION_PATH,
    SOURCE_LIFECYCLE_PATH,
    validate_source_authority,
)
from open_brain_engine.portable.v7 import validated_portable_snapshot_v7
from open_brain_engine.portable.v8 import (
    HISTORICAL_AUTHORITY_PATH,
    validate_historical_authority,
    validate_source_authority_v8,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_continuity import _adopt, _successor
from packages.app.tests.unit.engine.test_historical_link import _link_request
from packages.app.tests.unit.engine.test_historical_revocation import _request as _revocation


def _files(engine: BrainEngine) -> dict[str, bytes]:
    """Actual snapshot witnesses; this is not a substitute for full archive validation."""
    with closing(engine._store.connect()) as connection:
        admission: dict[str, object] = {
            "schema_version": 1,
            "control_epoch": connection.execute(
                "SELECT control_epoch FROM engine_generations"
            ).fetchone()[0],
        }
        for table, columns in AUTHORITY_COLUMNS.items():
            names = [item.split(":")[0] for item in columns]
            actual = "source_revisions" if table == "revision_admission" else table
            order = "delivery_id" if table == "source_intakes" else names[0]
            rows = []
            for row in connection.execute(
                f"SELECT {','.join(names)} FROM {actual} ORDER BY {order}"  # noqa: S608
            ):
                result = dict(row)
                for column in columns:
                    name, kind = column.split(":")
                    if kind == "b":
                        result[name] = base64.b64encode(result[name]).decode("ascii")
                rows.append(result)
            admission[table] = rows
        lifecycle = {
            "schema_version": 1,
            "source_lifecycle_state": admission.pop("source_lifecycle_state"),
            "source_lifecycle_operations": admission.pop("source_lifecycle_operations"),
        }
        identity = dict(connection.execute("SELECT * FROM brain_identity").fetchone())
        files = {
            "brain.toml": (engine.profile.root / "brain.toml").read_bytes(),
            SOURCE_METADATA_PATH: canonical(source_metadata(connection)),
            SOURCE_ADMISSION_PATH: canonical(admission),
            SOURCE_LIFECYCLE_PATH: canonical(lifecycle),
            ISSUER_MIGRATION_PATH: canonical(
                {
                    "tenant_id": identity["tenant_id"],
                    "brain_id": identity["brain_id"],
                    "current_issuer_epoch": identity["issuer_epoch"],
                }
            ),
            **{
                row["source_path"]: bytes(row["source_bytes"])
                for row in connection.execute(
                    "SELECT source_path,source_bytes FROM source_revisions"
                )
            },
        }
        path, raw = historical_authority_sidecar(connection, engine.profile)
        files[path] = raw
    return files


def _linked(engine: BrainEngine) -> HistoricalCopyRelationRequest:
    request, consent = _link_request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        link_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            load_consent=lambda: consent,
        )
    return request


def test_empty_authority_is_explicit_and_canonical(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    files = _files(engine)
    validated = validate_historical_authority(files)
    assert validated.registry.generation == 0 and validated.records == ()
    assert (
        canonical(json.loads(files[HISTORICAL_AUTHORITY_PATH])) == files[HISTORICAL_AUTHORITY_PATH]
    )
    assert set(json.loads(files[HISTORICAL_AUTHORITY_PATH])) == {
        "schema_version",
        "registry",
        "operations",
    }


def test_portable_first_import_in_clean_interpreter_has_no_engine_cycle() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from open_brain_engine.portable.versioned import "
            "validated_portable_snapshot; from open_brain_engine.portable.v8 import "
            "validate_historical_authority",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_actual_v8_export_preserves_baseline_predecessor_and_refuses_v7_reader(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    successor = _successor(baseline)
    engine.sources.public_revision_sink(successor.binding).submit(successor)
    destination = tmp_path / "export"
    receipt = engine.portability.export(destination, export_id="export_" + str(uuid4()))
    assert receipt.schema_version == 8
    snapshot = validated_portable_snapshot(destination)
    authority = validate_historical_authority(snapshot.files)
    assert authority.records[0].request == baseline
    admissions = validate_source_authority_v8(snapshot.files)
    old = next(
        row
        for row in admissions["revision_admission"]
        if row["capture_id"] == baseline.retained_original.capture_id
    )
    assert old["revision_key"] is None
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v7(destination)


@pytest.mark.parametrize("state", ["active", "revoked", "withdrawn", "advanced"])
def test_actual_linked_export_preserves_history_after_eligibility_changes(
    tmp_path: Path, state: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _linked(engine)
    before = validate_historical_authority(_files(engine))
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    if state == "revoked":
        with exclusive_runtime_admission(engine.profile) as admission:
            revoke_historical_copy(
                engine.profile,
                _revocation(engine, relation),
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
    elif state == "withdrawn":
        engine.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.portable.export",
                source_id=relation.source_cas.source_id,
                expected_head=relation.source_cas.expected_head,
                expected_lifecycle_version=relation.source_cas.expected_lifecycle_version,
                brain_id=relation.destination.brain_id,
                issuer_epoch=relation.destination.issuer_epoch,
                reason_code="synthetic_withdrawal",
            ),
            authority=owner,
        )
    elif state == "advanced":
        baseline = before.records[0].request
        from open_brain_engine.engine.historical_contracts import HistoricalBaselineRequest

        assert isinstance(baseline, HistoricalBaselineRequest)
        successor = _successor(baseline)
        engine.sources.public_revision_sink(successor.binding).submit(successor)
    expected = validate_historical_authority(_files(engine))
    destination = tmp_path / "export"
    assert (
        engine.portability.export(destination, export_id="export_" + str(uuid4())).schema_version
        == 8
    )
    snapshot = validated_portable_snapshot(destination)
    assert validate_historical_authority(snapshot.files) == expected
    assert expected.records[:3] == before.records
    assert b"consent" not in snapshot.files[HISTORICAL_AUTHORITY_PATH]


def test_frozen_v5_serializer_still_refuses_schema13(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    with (
        closing(engine._store.connect()) as connection,
        pytest.raises(ValueError, match="supported current state schema"),
    ):
        serialize_portable_v5_state(
            connection, tenant_id=engine.profile.tenant_id, relationship_sidecar_present=False
        )


@pytest.mark.parametrize("managed", [False, True])
def test_v8_proves_historical_predecessor_but_v6_still_refuses(
    tmp_path: Path, managed: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    successor = _successor(baseline)
    if managed:
        engine.sources.public_revision_sink(successor.binding).submit(successor)
    else:
        engine.sources.submit_revision(successor.submission)
    files = _files(engine)
    old_admission_bytes = files[SOURCE_ADMISSION_PATH]
    with pytest.raises(PortableValidationError, match="v6 source authority invalid"):
        validate_source_authority(files)
    validated = validate_source_authority_v8(files)
    old = next(
        row
        for row in validated["revision_admission"]
        if row["capture_id"] == baseline.retained_original.capture_id
    )
    assert old["revision_key"] is None
    assert files[SOURCE_ADMISSION_PATH] == old_admission_bytes


@pytest.mark.parametrize("withdraw", [False, True])
def test_revocation_and_withdrawal_preserve_exact_chain_without_consent(
    tmp_path: Path, withdraw: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _linked(engine)
    before = validate_historical_authority(_files(engine))
    request = _revocation(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        revoke_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    if withdraw:
        engine.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.portable.original",
                source_id=request.source_cas.source_id,
                expected_head=request.source_cas.expected_head,
                expected_lifecycle_version=request.source_cas.expected_lifecycle_version,
                brain_id=request.destination.brain_id,
                issuer_epoch=request.destination.issuer_epoch,
                reason_code="synthetic_withdrawal",
            ),
            authority=owner,
        )
    files = _files(engine)
    validated = validate_historical_authority(files)
    assert validated.records[:-1] == before.records
    assert validated.registry.memberships == before.registry.memberships
    assert validated.registry.generation == 4
    assert validated.records[-1].request == request
    assert b"consent" not in files[HISTORICAL_AUTHORITY_PATH]
    # Compact operations reconstruct the exact original transition snapshots.
    assert all(
        set(row)
        == {
            "kind",
            "request",
            "receipt",
            "previous_registry_sha256",
            "proposed_registry_sha256",
            "transition_sha256",
        }
        for row in json.loads(files[HISTORICAL_AUTHORITY_PATH])["operations"]
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unknown_field",
        "bool_version",
        "missing_revocation",
        "duplicate_operation",
        "operation_hash",
        "registry_hash",
        "foreign_destination",
        "alias",
        "privacy",
        "body",
        "capture_source",
        "namespace",
        "rewritten_baseline_key",
    ],
)
def test_required_sidecar_cross_witness_damage_refuses(tmp_path: Path, damage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _linked(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        revoke_historical_copy(
            engine.profile,
            _revocation(engine, relation),
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    files = _files(engine)
    value = json.loads(files[HISTORICAL_AUTHORITY_PATH])
    if damage == "missing":
        files.pop(HISTORICAL_AUTHORITY_PATH)
    elif damage == "alias":
        admission = json.loads(files[SOURCE_ADMISSION_PATH])
        admission["source_aliases"].clear()
        files[SOURCE_ADMISSION_PATH] = canonical(admission)
    elif damage == "rewritten_baseline_key":
        admission = json.loads(files[SOURCE_ADMISSION_PATH])
        capture_id = value["operations"][0]["request"]["retained_original"]["capture_id"]
        next(row for row in admission["revision_admission"] if row["capture_id"] == capture_id)[
            "revision_key"
        ] = "rewritten"
        files[SOURCE_ADMISSION_PATH] = canonical(admission)
    elif damage in ("body", "privacy", "capture_source"):
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        capture_id = value["operations"][0]["request"]["retained_original"]["capture_id"]
        revision = next(row for row in metadata["revisions"] if row["capture_id"] == capture_id)
        if damage == "capture_source":
            revision["source_id"] = "source_" + "b" * 32
            files[SOURCE_METADATA_PATH] = canonical(metadata)
        elif damage == "body":
            files[revision["source_path"]] += b" "
        else:
            record = json.loads(files[revision["source_path"]])
            record["privacy"]["tier"] = "public"
            files[revision["source_path"]] = canonical(record)
    else:
        if damage == "unknown_field":
            value["consent"] = []
        elif damage == "bool_version":
            value["schema_version"] = True
        elif damage == "missing_revocation":
            value["operations"].pop()
        elif damage == "duplicate_operation":
            value["operations"].append(value["operations"][-1])
        elif damage == "operation_hash":
            value["operations"][-1]["transition_sha256"] = "b" * 64
        elif damage == "registry_hash":
            value["registry"]["registry_sha256"] = "b" * 64
        elif damage == "foreign_destination":
            value["operations"][0]["request"]["destination"]["issuer_epoch"] += 1
        else:
            value["operations"][0]["request"]["observed_delivery"]["binding"]["namespace"][
                "external_id"
            ] = "different"
        files[HISTORICAL_AUTHORITY_PATH] = canonical(value)
    with pytest.raises(PortableValidationError):
        validate_historical_authority(files)
